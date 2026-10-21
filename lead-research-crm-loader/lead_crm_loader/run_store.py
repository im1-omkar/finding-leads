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
        self.users: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, tuple[str, datetime]] = {}
        self.auth_tokens: dict[str, tuple[str, str, datetime]] = {}
        self._event_id = 0
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def create_run(self, run_id: str, request: dict[str, Any], owner_id: str | None = None) -> None:
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
                "owner_id": owner_id,
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

    async def create_user(self, user_id: str, email: str, encoded_password: str) -> dict[str, Any] | None:
        if any(user["email"] == email for user in self.users.values()):
            return None
        user = {"user_id": user_id, "email": email, "password_hash": encoded_password, "email_verified": True}
        self.users[user_id] = user
        return dict(user)

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        return next((dict(user) for user in self.users.values() if user["email"] == email), None)

    async def create_session(self, user_id: str, token_hash: str, expires_at: datetime) -> None:
        self.sessions[token_hash] = (user_id, expires_at)

    async def get_user_for_session(self, token_hash: str) -> dict[str, Any] | None:
        session = self.sessions.get(token_hash)
        if not session or session[1] <= _now():
            self.sessions.pop(token_hash, None)
            return None
        user = self.users.get(session[0])
        return dict(user) if user else None

    async def delete_session(self, token_hash: str) -> None:
        self.sessions.pop(token_hash, None)

    async def create_auth_token(self, user_id: str, token_hash: str, purpose: str, expires_at: datetime) -> None:
        self.auth_tokens[token_hash] = (user_id, purpose, expires_at)

    async def consume_auth_token(self, token_hash: str, purpose: str) -> str | None:
        item = self.auth_tokens.get(token_hash)
        if item and item[1] == purpose and item[2] > _now():
            self.auth_tokens.pop(token_hash, None)
            return item[0]
        if item and item[2] <= _now():
            self.auth_tokens.pop(token_hash, None)
        return None

    async def mark_email_verified(self, user_id: str) -> None:
        if user_id in self.users:
            self.users[user_id]["email_verified"] = True

    async def set_password(self, user_id: str, encoded_password: str) -> None:
        if user_id in self.users:
            self.users[user_id]["password_hash"] = encoded_password
            self.sessions = {key: value for key, value in self.sessions.items() if value[0] != user_id}

    async def list_runs(self, owner_id: str, limit: int, offset: int, status_filter: str | None = None) -> tuple[list[dict[str, Any]], int]:
        rows = [run for run in self.runs.values() if run.get("owner_id") == owner_id and (not status_filter or run["status"] == status_filter)]
        rows.sort(key=lambda run: run["created_at"], reverse=True)
        return [dict(row) for row in rows[offset:offset + limit]], len(rows)

    async def owns_run(self, run_id: str, owner_id: str) -> bool:
        run = self.runs.get(run_id)
        return bool(run and run.get("owner_id") == owner_id)


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
                    """CREATE TABLE IF NOT EXISTS app_users (
                        user_id VARCHAR(64) PRIMARY KEY,
                        email VARCHAR(320) NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        email_verified BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS app_sessions (
                        token_hash CHAR(64) PRIMARY KEY,
                        user_id VARCHAR(64) NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
                        expires_at TIMESTAMPTZ NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS app_auth_tokens (
                        token_hash CHAR(64) PRIMARY KEY,
                        user_id VARCHAR(64) NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
                        purpose VARCHAR(32) NOT NULL,
                        expires_at TIMESTAMPTZ NOT NULL,
                        consumed_at TIMESTAMPTZ
                    )"""
                )
                await cur.execute("ALTER TABLE lead_runs ADD COLUMN IF NOT EXISTS owner_id VARCHAR(64) REFERENCES app_users(user_id) ON DELETE SET NULL")
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
                await cur.execute("CREATE INDEX IF NOT EXISTS idx_lead_runs_owner_created ON lead_runs(owner_id, created_at DESC)")
            await conn.commit()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def create_run(self, run_id: str, request: dict[str, Any], owner_id: str | None = None) -> None:
        from psycopg.types.json import Jsonb

        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO lead_runs (run_id, request_id, status, request, owner_id) VALUES (%s, %s, 'queued', %s, %s)",
                    (run_id, request["request_id"], Jsonb(request), owner_id),
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

    async def create_user(self, user_id: str, email: str, encoded_password: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO app_users (user_id,email,password_hash,email_verified) VALUES (%s,%s,%s,TRUE) ON CONFLICT (email) DO NOTHING RETURNING user_id,email,password_hash,email_verified",
                    (user_id, email, encoded_password),
                )
                row = await cur.fetchone()
            await conn.commit()
        return dict(zip(("user_id", "email", "password_hash", "email_verified"), row, strict=True)) if row else None

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT user_id,email,password_hash,email_verified FROM app_users WHERE email=%s", (email,))
                row = await cur.fetchone()
        return dict(zip(("user_id", "email", "password_hash", "email_verified"), row, strict=True)) if row else None

    async def create_session(self, user_id: str, token_hash: str, expires_at: datetime) -> None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("INSERT INTO app_sessions(token_hash,user_id,expires_at) VALUES (%s,%s,%s)", (token_hash,user_id,expires_at))
            await conn.commit()

    async def get_user_for_session(self, token_hash: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""SELECT u.user_id,u.email,u.password_hash,u.email_verified
                    FROM app_sessions s JOIN app_users u USING(user_id)
                    WHERE s.token_hash=%s AND s.expires_at>CURRENT_TIMESTAMP""", (token_hash,))
                row = await cur.fetchone()
        return dict(zip(("user_id", "email", "password_hash", "email_verified"), row, strict=True)) if row else None

    async def delete_session(self, token_hash: str) -> None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM app_sessions WHERE token_hash=%s", (token_hash,))
            await conn.commit()

    async def create_auth_token(self, user_id: str, token_hash: str, purpose: str, expires_at: datetime) -> None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("INSERT INTO app_auth_tokens(token_hash,user_id,purpose,expires_at) VALUES (%s,%s,%s,%s)", (token_hash,user_id,purpose,expires_at))
            await conn.commit()

    async def consume_auth_token(self, token_hash: str, purpose: str) -> str | None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""DELETE FROM app_auth_tokens WHERE token_hash=%s AND purpose=%s
                    AND expires_at>CURRENT_TIMESTAMP RETURNING user_id""", (token_hash,purpose))
                row = await cur.fetchone()
            await conn.commit()
        return row[0] if row else None

    async def mark_email_verified(self, user_id: str) -> None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE app_users SET email_verified=TRUE WHERE user_id=%s", (user_id,))
            await conn.commit()

    async def set_password(self, user_id: str, encoded_password: str) -> None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE app_users SET password_hash=%s WHERE user_id=%s", (encoded_password,user_id))
                await cur.execute("DELETE FROM app_sessions WHERE user_id=%s", (user_id,))
            await conn.commit()

    async def list_runs(self, owner_id: str, limit: int, offset: int, status_filter: str | None = None) -> tuple[list[dict[str, Any]], int]:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                # Avoid an untyped NULL bind in the optional status predicate. PostgreSQL
                # cannot infer the type of ``%s IS NULL`` for the default history view.
                if status_filter is None:
                    await cur.execute("SELECT COUNT(*) FROM lead_runs WHERE owner_id=%s", (owner_id,))
                else:
                    await cur.execute("SELECT COUNT(*) FROM lead_runs WHERE owner_id=%s AND status=%s", (owner_id,status_filter))
                total = (await cur.fetchone())[0]
                if status_filter is None:
                    await cur.execute("""SELECT run_id,request_id,status,request,report,error,created_at
                        FROM lead_runs WHERE owner_id=%s
                        ORDER BY created_at DESC LIMIT %s OFFSET %s""", (owner_id,limit,offset))
                else:
                    await cur.execute("""SELECT run_id,request_id,status,request,report,error,created_at
                        FROM lead_runs WHERE owner_id=%s AND status=%s
                        ORDER BY created_at DESC LIMIT %s OFFSET %s""", (owner_id,status_filter,limit,offset))
                rows = await cur.fetchall()
        keys = ("run_id", "request_id", "status", "request", "report", "error", "created_at")
        results = []
        for row in rows:
            item = dict(zip(keys,row,strict=True))
            item["created_at"] = item["created_at"].isoformat() if hasattr(item["created_at"],"isoformat") else item["created_at"]
            results.append(item)
        return results, total

    async def owns_run(self, run_id: str, owner_id: str) -> bool:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM lead_runs WHERE run_id=%s AND owner_id=%s", (run_id,owner_id))
                return await cur.fetchone() is not None
