from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol

import httpx

from .config import Settings
from .deduplication import normalize_company_name
from .models import Candidate
from .sources import normalized_domain


class CRMAdapter(Protocol):
    async def upsert_company(self, candidate: Candidate, idempotency_key: str, request_region: str) -> dict[str, Any]: ...

    async def get_company(self, record_id: str) -> dict[str, Any] | None: ...


class MockCRM:
    """SQLite-backed mock CRM; record writes are idempotent and visible via the API."""

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def __init__(self, sqlite_path: str) -> None:
        self.sqlite_path = sqlite_path
        self._lock = asyncio.Lock()
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS mock_companies (
                    record_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    domain TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    properties_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mock_companies_domain ON mock_companies(domain)")
            conn.commit()

    @staticmethod
    def _record(row: tuple[Any, ...]) -> dict[str, Any]:
        record_id, domain, properties_json, created_at = row
        return {
            "id": record_id,
            "domain": domain,
            "properties": json.loads(properties_json),
            "created_at": created_at,
            "url": f"/mock-crm/companies/{record_id}",
        }

    async def upsert_company(self, candidate: Candidate, idempotency_key: str, request_region: str) -> dict[str, Any]:
        properties = {
            "name": candidate.legal_name or candidate.company_name,
            "domain": candidate.domain,
            "website": candidate.website_url,
            "city": request_region,
            "description": candidate.rationale,
            "lead_source_url": candidate.source_evidence[0].source_url if candidate.source_evidence else candidate.website_url,
            "qualification_confidence": candidate.confidence,
            "source_evidence_ids": [e.evidence_id for e in candidate.source_evidence],
            "candidate_id": candidate.candidate_id,
        }
        async with self._lock:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                existing = conn.execute(
                    "SELECT record_id, domain, properties_json, created_at FROM mock_companies WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    return {"status": "already_exists", **self._record(tuple(existing))}

                existing = conn.execute(
                    "SELECT record_id, domain, properties_json, created_at FROM mock_companies WHERE domain=? OR normalized_name=? LIMIT 1",
                    (candidate.domain, normalize_company_name(candidate.legal_name or candidate.company_name)),
                ).fetchone()
                if existing:
                    return {"status": "already_exists", **self._record(tuple(existing))}

                record_id = "mock_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:18]
                conn.execute(
                    "INSERT INTO mock_companies(record_id,idempotency_key,domain,normalized_name,properties_json) VALUES(?,?,?,?,?)",
                    (
                        record_id,
                        idempotency_key,
                        candidate.domain,
                        normalize_company_name(candidate.legal_name or candidate.company_name),
                        json.dumps(properties, ensure_ascii=False),
                    ),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT record_id, domain, properties_json, created_at FROM mock_companies WHERE record_id=?",
                    (record_id,),
                ).fetchone()
                return {"status": "created", **self._record(tuple(row))}

    async def get_company(self, record_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT record_id, domain, properties_json, created_at FROM mock_companies WHERE record_id=?",
                (record_id,),
            ).fetchone()
        return self._record(row) if row else None

    async def list_companies(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT record_id, domain, properties_json, created_at FROM mock_companies ORDER BY created_at DESC"
            ).fetchall()
        return [self._record(row) for row in rows]


class HubSpotCRM:
    """HubSpot company adapter using the current date-versioned CRM object API."""

    PROPERTIES = ["name", "domain", "website", "city", "description", "numberofemployees"]

    def __init__(self, access_token: str, api_version: str = "2026-09", portal_id: str | None = None, timeout: float = 12.0) -> None:
        self.api_version = api_version
        self.portal_id = portal_id
        self.timeout = timeout
        self.client = httpx.AsyncClient(
            base_url="https://api.hubapi.com",
            timeout=timeout,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        )
        self._lock = asyncio.Lock()

    def _url(self, record_id: str) -> str | None:
        if not self.portal_id:
            return None
        return f"https://app.hubspot.com/contacts/{self.portal_id}/record/0-2/{record_id}"

    async def _search(self, property_name: str, value: str) -> dict[str, Any] | None:
        response = await self.client.post(
            f"/crm/objects/{self.api_version}/companies/search",
            json={
                "filterGroups": [{"filters": [{"propertyName": property_name, "operator": "EQ", "value": value}]}],
                "properties": self.PROPERTIES,
                "limit": 5,
            },
        )
        response.raise_for_status()
        rows = response.json().get("results", [])
        return rows[0] if rows else None

    async def upsert_company(self, candidate: Candidate, idempotency_key: str, request_region: str) -> dict[str, Any]:
        # Serializes writes within this process. Searching before creating makes ordinary retries
        # idempotent even if the API created a record but the caller lost the response.
        async with self._lock:
            existing = await self._search("domain", candidate.domain)
            if not existing:
                existing = await self._search("name", candidate.legal_name or candidate.company_name)
            if existing:
                return {
                    "status": "already_exists",
                    "id": str(existing["id"]),
                    "properties": existing.get("properties", {}),
                    "url": self._url(str(existing["id"])),
                }

            evidence_urls = list(dict.fromkeys(e.source_url for e in candidate.source_evidence))[:5]
            description = (
                f"Lead Research & CRM Loader\nQualification: {candidate.rationale}\n"
                f"Confidence: {candidate.confidence:.2f}\nSource URLs: " + ", ".join(evidence_urls)
            )[:4000]
            properties: dict[str, Any] = {
                "name": candidate.legal_name or candidate.company_name,
                "domain": candidate.domain,
                "website": candidate.website_url,
                "city": request_region,
                "description": description,
            }
            if candidate.employee_count is not None:
                properties["numberofemployees"] = candidate.employee_count
            response = await self.client.post(
                f"/crm/objects/{self.api_version}/0-2",
                json={"properties": properties},
            )
            response.raise_for_status()
            body = response.json()
            return {
                "status": "created",
                "id": str(body["id"]),
                "properties": body.get("properties", properties),
                "url": self._url(str(body["id"])),
            }

    async def get_company(self, record_id: str) -> dict[str, Any] | None:
        response = await self.client.get(
            f"/crm/objects/{self.api_version}/0-2/{record_id}",
            params={"properties": ",".join(self.PROPERTIES)},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = response.json()
        return {"id": str(body["id"]), "properties": body.get("properties", {}), "url": self._url(str(body["id"]))}

    async def aclose(self) -> None:
        await self.client.aclose()


def build_crm_adapter(settings: Settings) -> CRMAdapter:
    if settings.mock_mode:
        return MockCRM(settings.crm_sqlite_path)
    if not settings.hubspot_access_token:
        raise RuntimeError("HUBSPOT_ACCESS_TOKEN is required when MOCK_MODE=false")
    import os

    return HubSpotCRM(
        access_token=settings.hubspot_access_token,
        api_version=settings.hubspot_api_version,
        portal_id=os.getenv("HUBSPOT_PORTAL_ID"),
        timeout=settings.request_timeout_seconds,
    )
