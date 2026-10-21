from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

from .auth import (
    CSRF_COOKIE,
    DUMMY_PASSWORD_HASH,
    SESSION_COOKIE,
    clear_session_cookies,
    current_user,
    hash_token,
    issue_session,
    new_user_id,
    password_hash,
    password_matches,
    send_auth_email,
    validate_csrf,
)

from .config import Settings
from .crm import PostgresCRM, build_crm_adapter
from .execution import execute_run_job
from .graph import create_graph
from .models import ApprovalDecision, LeadRequest
from .run_store import InMemoryRunStore, PostgresRunStore
from .sources import build_source_adapters


class ApprovalBody(BaseModel):
    approved: bool
    proposal_hash: str
    approved_candidate_ids: list[str] = Field(default_factory=list)
    comment: str | None = Field(default=None, max_length=2000)


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=10, max_length=256)

class TokenBody(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class EmailBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ResetBody(TokenBody):
    password: str = Field(min_length=10, max_length=256)


def _email(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    return normalized


def _configure_langsmith(settings: Settings) -> None:
    if settings.langsmith_tracing and settings.langsmith_api_key:
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if os.getenv("APP_ENV", "development").lower() == "production" and len(os.getenv("APP_SESSION_SECRET", "")) < 32:
        raise RuntimeError("Production requires APP_SESSION_SECRET to contain at least 32 characters.")
    if not settings.mock_mode:
        settings.validate_for_live_mode()
    _configure_langsmith(settings)

    search_provider, website_reader = build_source_adapters(settings)
    crm_adapter = build_crm_adapter(settings)
    use_postgres = settings.database_url.startswith(("postgres://", "postgresql://"))
    store: PostgresRunStore | InMemoryRunStore = (
        PostgresRunStore(settings.database_url) if use_postgres else InMemoryRunStore()
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.setup()
        if isinstance(crm_adapter, PostgresCRM):
            await crm_adapter.setup()
        if not use_postgres:
            app.state.graph = create_graph(
                settings=settings,
                search_provider=search_provider,
                website_reader=website_reader,
                crm_adapter=crm_adapter,
                checkpointer=InMemorySaver(),
            )
        try:
            yield
        finally:
            for task in list(app.state.local_tasks):
                task.cancel()
            if app.state.local_tasks:
                await asyncio.gather(*app.state.local_tasks, return_exceptions=True)
            await crm_adapter.aclose()
            await store.close()

    app = FastAPI(
        title="Lead Research & CRM Loader Agent",
        version="0.2.0",
        description="Evidence-backed company research with explicit approval before CRM writes.",
        lifespan=lifespan,
    )
    origins = [origin.strip() for origin in os.getenv("FRONTEND_ORIGINS", "http://localhost:3000").split(",") if origin.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID", "X-CSRF-Token"],
    )
    app.state.settings = settings
    app.state.crm_adapter = crm_adapter
    app.state.run_store = store
    app.state.auth_store = store
    app.state.local_tasks = set()

    async def execute_local_job(job: dict[str, Any]) -> None:
        try:
            await execute_run_job(job, app.state.graph, store, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            await store.set_run(job["run_id"], status="failed", error=message)
            await store.append_event(
                job["run_id"],
                {"node": "worker", "status": "failed", "message": "Run failed; see error details.", "error": message},
            )

    def start_local_task(job: dict[str, Any]) -> None:
        task = asyncio.create_task(execute_local_job(job))
        app.state.local_tasks.add(task)
        task.add_done_callback(app.state.local_tasks.discard)

    # health check of application
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": "mock" if settings.mock_mode else "live"}

    @app.get("/config")
    async def public_config() -> dict[str, Any]:
        return {
            "number_of_leads": settings.number_of_leads,
            "test_mode_browserbase_call_limit": settings.test_mode_browserbase_call_limit,
            "test_mode_llm_call_limit": settings.test_mode_llm_call_limit,
            "test_mode_candidate_url_limit": settings.test_mode_candidate_url_limit,
            "mock_mode": settings.mock_mode,
            "browserbase_configured": bool(settings.browserbase_api_key),
            "llm_configured": bool(settings.llm_api_key),
            "langsmith_enabled": settings.langsmith_tracing and bool(settings.langsmith_api_key),
        }

    @app.post("/auth/register", status_code=201)
    async def register(body: Credentials, request: Request, response: Response) -> dict[str, Any]:
        email = _email(body.email)
        user = await store.create_user(new_user_id(), email, password_hash(body.password))
        if not user:
            raise HTTPException(status_code=409, detail="An account with this email already exists.")
        await issue_session(store, user["user_id"], response, os.getenv("APP_ENV", "development").lower() == "production")
        return {"message": "Account created.", "user": {"user_id": user["user_id"], "email": user["email"]}}

    @app.post("/auth/login")
    async def login(body: Credentials, request: Request, response: Response) -> dict[str, Any]:
        email = _email(body.email)
        user = await store.get_user_by_email(email)
        # Always perform a password hash verification when the user exists; never expose account state.
        password_valid = password_matches(user["password_hash"] if user else DUMMY_PASSWORD_HASH, body.password)
        if not user or not password_valid:
            raise HTTPException(status_code=401, detail="Email or password is incorrect.")
        await issue_session(store, user["user_id"], response, os.getenv("APP_ENV", "development").lower() == "production")
        return {"user": {"user_id": user["user_id"], "email": user["email"]}}

    @app.post("/auth/logout")
    async def logout(request: Request, response: Response) -> dict[str, str]:
        user = await current_user(request)
        validate_csrf(request)
        session_token = request.cookies.get(SESSION_COOKIE)
        if session_token:
            await store.delete_session(hash_token(session_token))
        clear_session_cookies(response, os.getenv("APP_ENV", "development").lower() == "production")
        return {"message": "Signed out."}

    @app.get("/auth/me")
    async def me(request: Request) -> dict[str, Any]:
        user = await current_user(request)
        return {"user_id": user["user_id"], "email": user["email"]}

    @app.post("/auth/verify-email")
    async def verify_email(body: TokenBody) -> dict[str, str]:
        user_id = await store.consume_auth_token(hash_token(body.token), "verify")
        if not user_id:
            raise HTTPException(status_code=400, detail="Verification link is invalid or expired.")
        await store.mark_email_verified(user_id)
        return {"message": "Email verified. You can now sign in."}

    @app.post("/auth/resend-verification")
    async def resend_verification(body: EmailBody, request: Request) -> dict[str, str]:
        email = _email(body.email)
        user = await store.get_user_by_email(email)
        if user and not user["email_verified"]:
            token = secrets.token_urlsafe(36)
            from datetime import datetime, timedelta, timezone
            await store.create_auth_token(user["user_id"], hash_token(token), "verify", datetime.now(timezone.utc) + timedelta(minutes=30))
            await send_auth_email(request, email, "Verify your Lead Research account", token, "verify-email")
        return {"message": "If the account needs verification, a new link has been sent."}

    @app.post("/auth/forgot-password")
    async def forgot_password(body: EmailBody, request: Request) -> dict[str, str]:
        email = _email(body.email)
        user = await store.get_user_by_email(email)
        if user:
            token = secrets.token_urlsafe(36)
            from datetime import datetime, timedelta, timezone
            await store.create_auth_token(user["user_id"], hash_token(token), "reset", datetime.now(timezone.utc) + timedelta(minutes=30))
            await send_auth_email(request, email, "Reset your Lead Research password", token, "reset-password")
        return {"message": "If the account exists, a reset link has been sent."}

    @app.post("/auth/reset-password")
    async def reset_password(body: ResetBody) -> dict[str, str]:
        user_id = await store.consume_auth_token(hash_token(body.token), "reset")
        if not user_id:
            raise HTTPException(status_code=400, detail="Password reset link is invalid or expired.")
        await store.set_password(user_id, password_hash(body.password))
        return {"message": "Password updated. Sign in with your new password."}

    @app.get("/runs")
    async def list_runs(request: Request, limit: int = 20, offset: int = 0, status_filter: str | None = None) -> dict[str, Any]:
        user = await current_user(request)
        if limit < 1 or limit > 100 or offset < 0:
            raise HTTPException(status_code=422, detail="Invalid pagination.")
        rows, total = await store.list_runs(user["user_id"], limit, offset, status_filter)
        return {"results": rows, "total": total, "limit": limit, "offset": offset}

    @app.post("/runs", status_code=202)
    async def start_run(request_body: LeadRequest, request: Request) -> dict[str, Any]:
        user = await current_user(request)
        validate_csrf(request)
        if request_body.test_mode:
            try:
                settings.validate_for_live_mode()
            except RuntimeError as exc:
                raise HTTPException(status_code=422, detail=f"Low-call test mode needs live provider credentials. {exc}") from exc
        run_id = str(uuid.uuid4())
        authoritative_request = request_body.model_copy(update={"target_leads": settings.number_of_leads})
        run_payload = authoritative_request.model_dump(mode="json")
        await store.create_run(run_id, run_payload, user["user_id"])
        if not use_postgres:
            start_local_task({"job_type": "start", "run_id": run_id, "payload": {"request": run_payload}})
        return {
            "run_id": run_id,
            "status": "queued",
            "target_leads": settings.number_of_leads,
            "events_url": f"/runs/{run_id}/events",
        }

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> dict[str, Any]:
        user = await current_user(request)
        if not await store.owns_run(run_id, user["user_id"]):
            raise HTTPException(status_code=404, detail="Run not found")
        run = await store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return {
            "run_id": run_id,
            "request_id": run["request_id"],
            "status": run["status"],
            "request": run["request"],
            "approval": run["approval"] if run["status"] == "awaiting_approval" else None,
            "candidates": run.get("results") or [],
            "report": run["report"],
            "error": run["error"],
            "created_at": run["created_at"],
        }

    @app.get("/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, after_id: int = 0) -> StreamingResponse:
        user = await current_user(request)
        if not await store.owns_run(run_id, user["user_id"]):
            raise HTTPException(status_code=404, detail="Run not found")
        try:
            cursor = max(after_id, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            cursor = after_id

        async def event_stream() -> AsyncIterator[str]:
            nonlocal cursor
            idle_ticks = 0
            while True:
                new_events = await store.events_after(run_id, cursor)
                for item in new_events:
                    cursor = item["id"]
                    yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(item['event'], ensure_ascii=False)}\n\n"
                run = await store.get_run(run_id)
                if run and run["status"] in {"awaiting_approval", "completed", "failed"} and not new_events:
                    yield f"event: end\ndata: {json.dumps({'status': run['status']})}\n\n"
                    break
                idle_ticks += 1
                if idle_ticks % 30 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/runs/{run_id}/events/history")
    async def run_event_history(run_id: str, request: Request, after_id: int = 0) -> dict[str, Any]:
        user = await current_user(request)
        if not await store.owns_run(run_id, user["user_id"]):
            raise HTTPException(status_code=404, detail="Run not found")
        events = await store.events_after(run_id, max(after_id, 0))
        return {"results": events, "last_event_id": events[-1]["id"] if events else max(after_id, 0)}

    @app.post("/runs/{run_id}/approval", status_code=202)
    async def resume_with_approval(run_id: str, body: ApprovalBody, request: Request) -> dict[str, Any]:
        user = await current_user(request)
        validate_csrf(request)
        try:
            decision = ApprovalDecision.model_validate({**body.model_dump(), "reviewer_id": user["email"]})
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not await store.owns_run(run_id, user["user_id"]):
            raise HTTPException(status_code=404, detail="Run not found")
        run = await store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        if run["status"] != "awaiting_approval":
            raise HTTPException(status_code=409, detail="Run is not waiting for approval")
        proposal = run.get("approval") or {}
        valid_ids = {item["candidate_id"] for item in proposal.get("changes", [])}
        if decision.proposal_hash != proposal.get("proposal_hash"):
            raise HTTPException(status_code=409, detail="Approval does not match the current proposal")
        if not set(decision.approved_candidate_ids).issubset(valid_ids):
            raise HTTPException(status_code=422, detail="Approval contains candidate IDs outside the proposal")
        if decision.approved and not decision.approved_candidate_ids:
            raise HTTPException(status_code=422, detail="Select at least one lead or reject the proposal")
        payload = decision.model_dump(mode="json")
        await store.enqueue_approval(run_id, payload)
        if not use_postgres:
            start_local_task({"job_type": "approval", "run_id": run_id, "payload": payload})
        return {"run_id": run_id, "status": "queued", "message": "Approval accepted; workflow is resuming."}

    @app.get("/runs/{run_id}/report")
    async def get_report(run_id: str, request: Request) -> dict[str, Any]:
        user = await current_user(request)
        if not await store.owns_run(run_id, user["user_id"]):
            raise HTTPException(status_code=404, detail="Run not found")
        run = await store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        if not run["report"]:
            raise HTTPException(status_code=409, detail="Report is not ready yet")
        return run["report"]

    @app.get("/crm/companies")
    async def list_companies(request: Request) -> dict[str, Any]:
        await current_user(request)
        return {"results": await request.app.state.crm_adapter.list_companies()}

    @app.get("/crm/companies/{record_id}")
    async def get_company(record_id: str, request: Request) -> dict[str, Any]:
        await current_user(request)
        record = await request.app.state.crm_adapter.get_company(record_id)
        if not record:
            raise HTTPException(status_code=404, detail="Company record not found")
        return record

    @app.get("/mock-crm/companies")
    async def list_mock_companies(request: Request) -> dict[str, Any]:
        return await list_companies(request)

    @app.get("/mock-crm/companies/{record_id}")
    async def get_mock_company(record_id: str, request: Request) -> dict[str, Any]:
        return await get_company(record_id, request)

    return app


app = create_app()
