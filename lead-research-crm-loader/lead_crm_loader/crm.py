from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Protocol

from .config import Settings
from .deduplication import normalize_company_name
from .models import Candidate


class CRMAdapter(Protocol):
    async def upsert_company(self, candidate: Candidate, idempotency_key: str, request_region: str) -> dict[str, Any]: ...

    async def get_company(self, record_id: str) -> dict[str, Any] | None: ...

    async def list_companies(self) -> list[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


def build_candidate_properties(candidate: Candidate, request_region: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
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
    if candidate.employee_count is not None:
        properties["numberofemployees"] = candidate.employee_count
    return properties


class InMemoryCRM:
    """In-memory CRM for testing and standalone local workflows without an external database."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._by_idempotency: dict[str, str] = {}
        self._by_domain: dict[str, str] = {}
        self._by_normalized_name: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def upsert_company(self, candidate: Candidate, idempotency_key: str, request_region: str) -> dict[str, Any]:
        properties = build_candidate_properties(candidate, request_region)
        norm_name = normalize_company_name(candidate.legal_name or candidate.company_name)
        async with self._lock:
            existing_id = self._by_idempotency.get(idempotency_key)
            if not existing_id:
                existing_id = self._by_domain.get(candidate.domain) or self._by_normalized_name.get(norm_name)
            if existing_id and existing_id in self._records:
                return {"status": "already_exists", **self._records[existing_id]}

            record_id = "crm_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:18]
            record = {
                "id": record_id,
                "domain": candidate.domain,
                "properties": properties,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "url": f"/crm/companies/{record_id}",
            }
            self._records[record_id] = record
            self._by_idempotency[idempotency_key] = record_id
            self._by_domain[candidate.domain] = record_id
            self._by_normalized_name[norm_name] = record_id
            return {"status": "created", **record}

    async def get_company(self, record_id: str) -> dict[str, Any] | None:
        return self._records.get(record_id)

    async def list_companies(self) -> list[dict[str, Any]]:
        return list(self._records.values())

    async def aclose(self) -> None:
        pass


# Alias for backward compatibility with existing tests
MockCRM = InMemoryCRM


class PostgresCRM:
    """PostgreSQL CRM adapter using connection pooling and JSONB storage for company leads."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        from psycopg_pool import AsyncConnectionPool

        if not self._pool:
            self._pool = AsyncConnectionPool(
                conninfo=self.database_url,
                open=False,
                min_size=1,
                max_size=10,
                timeout=30.0,
            )
            await self._pool.open()

        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS crm_companies (
                        record_id VARCHAR(64) PRIMARY KEY,
                        idempotency_key VARCHAR(128) UNIQUE NOT NULL,
                        domain VARCHAR(255) NOT NULL,
                        normalized_name VARCHAR(255) NOT NULL,
                        company_name VARCHAR(255) NOT NULL,
                        properties JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                await cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_companies_domain ON crm_companies(domain)")
                await cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_companies_normalized_name ON crm_companies(normalized_name)")
            await conn.commit()

    @staticmethod
    def _record(row: tuple[Any, ...]) -> dict[str, Any]:
        record_id, domain, company_name, props, created_at = row
        if isinstance(props, str):
            props = json.loads(props)
        created_str = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
        return {
            "id": record_id,
            "domain": domain,
            "company_name": company_name,
            "properties": props,
            "created_at": created_str,
            "url": f"/crm/companies/{record_id}",
        }

    async def upsert_company(self, candidate: Candidate, idempotency_key: str, request_region: str) -> dict[str, Any]:
        properties = build_candidate_properties(candidate, request_region)
        company_name = candidate.legal_name or candidate.company_name
        norm_name = normalize_company_name(company_name)

        if not self._pool:
            await self.setup()

        async with self._lock:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    # Serialize writes for the same domain/name across workers too.
                    # Sorted advisory-lock keys give all callers the same lock order.
                    lock_keys = sorted((f"domain:{candidate.domain}", f"name:{norm_name}"))
                    for lock_key in lock_keys:
                        await cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))

                    # 1. Check idempotency key
                    await cur.execute(
                        "SELECT record_id, domain, company_name, properties, created_at FROM crm_companies WHERE idempotency_key = %s",
                        (idempotency_key,),
                    )
                    row = await cur.fetchone()
                    if row:
                        return {"status": "already_exists", **self._record(row)}

                    # 2. Check domain or normalized company name
                    await cur.execute(
                        "SELECT record_id, domain, company_name, properties, created_at FROM crm_companies WHERE domain = %s OR normalized_name = %s LIMIT 1",
                        (candidate.domain, norm_name),
                    )
                    row = await cur.fetchone()
                    if row:
                        return {"status": "already_exists", **self._record(row)}

                    # 3. Insert new record
                    record_id = "crm_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:18]
                    from psycopg.types.json import Jsonb

                    await cur.execute(
                        """
                        INSERT INTO crm_companies (record_id, idempotency_key, domain, normalized_name, company_name, properties)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (idempotency_key) DO NOTHING
                        RETURNING record_id, domain, company_name, properties, created_at
                        """,
                        (record_id, idempotency_key, candidate.domain, norm_name, company_name, Jsonb(properties)),
                    )
                    row = await cur.fetchone()
                    await conn.commit()
                    if not row:
                        # Conflict occurred; fetch existing
                        await cur.execute(
                            "SELECT record_id, domain, company_name, properties, created_at FROM crm_companies WHERE idempotency_key = %s",
                            (idempotency_key,),
                        )
                        row = await cur.fetchone()
                        return {"status": "already_exists", **self._record(row)}
                    return {"status": "created", **self._record(row)}

    async def get_company(self, record_id: str) -> dict[str, Any] | None:
        if not self._pool:
            await self.setup()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT record_id, domain, company_name, properties, created_at FROM crm_companies WHERE record_id = %s",
                    (record_id,),
                )
                row = await cur.fetchone()
                return self._record(row) if row else None

    async def list_companies(self) -> list[dict[str, Any]]:
        if not self._pool:
            await self.setup()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT record_id, domain, company_name, properties, created_at FROM crm_companies ORDER BY created_at DESC"
                )
                rows = await cur.fetchall()
                return [self._record(row) for row in rows]

    async def aclose(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None


def build_crm_adapter(settings: Settings) -> CRMAdapter:
    if settings.database_url.startswith("memory://"):
        return InMemoryCRM()
    return PostgresCRM(settings.database_url)
