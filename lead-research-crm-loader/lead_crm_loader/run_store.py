from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryRunStore:
    """Small local/test run store. Durable deployments use PostgreSQLRunStore."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self._event_id = 0
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def create_run(self, run_id: str, request: dict[str, Any]) -> None:
        async with self._lock:
            self.runs[run_id] = {
                "run_id": run_id,
                "request_id": request["request_id"],
                "status": "queued",
                "request": request,
                "approval": None,
                "report": None,
                "error": None,
                "created_at": _now().isoformat(),
            }
        await self.append_event(run_id, {"node": "queued", "status": "queued", "message": "Research run queued."})

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.runs.get(run_id)

    async def set_run(self, run_id: str, **values: Any) -> None:
        async with self._lock:
            if run_id in self.runs:
                self.runs[run_id].update(values)

    async def append_event(self, run_id: str, event: dict[str, Any]) -> int:
        async with self._lock:
            self._event_id += 1
            self.events.setdefault(run_id, []).append({"id": self._event_id, "event": event})
            return self._event_id

    async def events_after(self, run_id: str, after_id: int) -> list[dict[str, Any]]:
        return [item for item in self.events.get(run_id, []) if item["id"] > after_id]

    async def enqueue_approval(self, run_id: str, payload: dict[str, Any]) -> None:
        await self.set_run(run_id, status="queued", approval=payload)


class PostgresRunStore:
    """PostgreSQL-backed run state, progress events, and a small durable job queue."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._pool: Any = None

    async def setup(self) -> None:
        from psycopg_pool import AsyncConnectionPool

        if self._pool is None:
            self._pool = AsyncConnectionPool(self.database_url, open=False, min_size=1, max_size=10, timeout=30)
            await self._pool.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS lead_runs (
                        run_id VARCHAR(64) PRIMARY KEY,
                        request_id VARCHAR(100) NOT NULL,
                        status VARCHAR(32) NOT NULL,
                        request JSONB NOT NULL,
                        approval JSONB,
                        results JSONB,
                        report JSONB,
                        error TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS lead_run_events (
                        event_id BIGSERIAL PRIMARY KEY,
                        run_id VARCHAR(64) NOT NULL REFERENCES lead_runs(run_id) ON DELETE CASCADE,
                        event JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS lead_run_jobs (
                        job_id BIGSERIAL PRIMARY KEY,
                        run_id VARCHAR(64) NOT NULL REFERENCES lead_runs(run_id) ON DELETE CASCADE,
                        job_type VARCHAR(32) NOT NULL,
                        payload JSONB NOT NULL,
                        status VARCHAR(16) NOT NULL DEFAULT 'queued',
                        claimed_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                await cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_lead_run_events_run_id_event_id ON lead_run_events(run_id, event_id)"
                )
                await cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_lead_run_jobs_queue ON lead_run_jobs(status, created_at)"
                )
                await cur.execute("ALTER TABLE lead_runs ADD COLUMN IF NOT EXISTS results JSONB")
            await conn.commit()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def create_run(self, run_id: str, request: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO lead_runs (run_id, request_id, status, request) VALUES (%s, %s, 'queued', %s)",
                    (run_id, request["request_id"], Jsonb(request)),
                )
                await cur.execute(
                    "INSERT INTO lead_run_jobs (run_id, job_type, payload) VALUES (%s, 'start', %s)",
                    (run_id, Jsonb({"request": request})),
                )
                await cur.execute(
                    "INSERT INTO lead_run_events (run_id, event) VALUES (%s, %s)",
                    (run_id, Jsonb({"node": "queued", "status": "queued", "message": "Research run queued."})),
                )
            await conn.commit()

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT run_id, request_id, status, request, approval, results, report, error, created_at FROM lead_runs WHERE run_id=%s",
                    (run_id,),
                )
                row = await cur.fetchone()
        if not row:
            return None
        keys = ("run_id", "request_id", "status", "request", "approval", "results", "report", "error", "created_at")
        result = dict(zip(keys, row, strict=True))
        if hasattr(result["created_at"], "isoformat"):
            result["created_at"] = result["created_at"].isoformat()
        return result

    async def set_run(self, run_id: str, **values: Any) -> None:
        from psycopg.types.json import Jsonb

        allowed = {"status", "approval", "results", "report", "error"}
        updates = [(key, value) for key, value in values.items() if key in allowed]
        if not updates:
            return
        assignments = ", ".join(f"{key}=%s" for key, _ in updates)
        params = [Jsonb(value) if key in {"approval", "results", "report"} and value is not None else value for key, value in updates]
        params.append(run_id)
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"UPDATE lead_runs SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE run_id=%s",
                    params,
                )
            await conn.commit()

    async def append_event(self, run_id: str, event: dict[str, Any]) -> int:
        from psycopg.types.json import Jsonb

        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO lead_run_events (run_id, event) VALUES (%s, %s) RETURNING event_id",
                    (run_id, Jsonb(event)),
                )
                row = await cur.fetchone()
            await conn.commit()
        return int(row[0])

    async def events_after(self, run_id: str, after_id: int) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT event_id, event FROM lead_run_events WHERE run_id=%s AND event_id>%s ORDER BY event_id LIMIT 500",
                    (run_id, after_id),
                )
                rows = await cur.fetchall()
        return [{"id": int(row[0]), "event": row[1]} for row in rows]

    async def enqueue_approval(self, run_id: str, payload: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO lead_run_jobs (run_id, job_type, payload) VALUES (%s, 'approval', %s)",
                    (run_id, Jsonb(payload)),
                )
                await cur.execute(
                    "UPDATE lead_runs SET status='queued', updated_at=CURRENT_TIMESTAMP WHERE run_id=%s",
                    (run_id,),
                )
                await cur.execute(
                    "INSERT INTO lead_run_events (run_id, event) VALUES (%s, %s)",
                    (run_id, Jsonb({"node": "approval", "status": "queued", "message": "Approval accepted; resuming research run."})),
                )
            await conn.commit()

    async def claim_job(self) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """WITH next_job AS (
                        SELECT job_id FROM lead_run_jobs WHERE status='queued'
                        ORDER BY created_at, job_id FOR UPDATE SKIP LOCKED LIMIT 1
                    )
                    UPDATE lead_run_jobs AS jobs
                    SET status='running', claimed_at=CURRENT_TIMESTAMP
                    FROM next_job WHERE jobs.job_id=next_job.job_id
                    RETURNING jobs.job_id, jobs.run_id, jobs.job_type, jobs.payload"""
                )
                row = await cur.fetchone()
            await conn.commit()
        if not row:
            return None
        return {"job_id": row[0], "run_id": row[1], "job_type": row[2], "payload": row[3]}

    async def heartbeat_job(self, job_id: int) -> None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE lead_run_jobs SET claimed_at=CURRENT_TIMESTAMP WHERE job_id=%s AND status='running'",
                    (job_id,),
                )
            await conn.commit()

    async def finish_job(self, job_id: int) -> None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE lead_run_jobs SET status='done' WHERE job_id=%s", (job_id,))
            await conn.commit()

    async def recover_stale_jobs(self) -> None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """WITH stale_jobs AS (
                        UPDATE lead_run_jobs
                        SET status='queued', claimed_at=NULL
                        WHERE status='running'
                          AND claimed_at < CURRENT_TIMESTAMP - INTERVAL '90 seconds'
                        RETURNING run_id
                    )
                    UPDATE lead_runs AS runs
                    SET status='queued', updated_at=CURRENT_TIMESTAMP
                    FROM stale_jobs
                    WHERE runs.run_id=stale_jobs.run_id AND runs.status='running'"""
                )
            await conn.commit()
