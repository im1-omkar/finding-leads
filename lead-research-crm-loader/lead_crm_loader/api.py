from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from .config import Settings
from .crm import HubSpotCRM, MockCRM, build_crm_adapter
from .graph import create_graph
from .models import ApprovalDecision, LeadRequest
from .sources import build_source_adapters


class ApprovalBody(BaseModel):
    approved: bool
    proposal_hash: str
    approved_candidate_ids: list[str] = Field(default_factory=list)
    reviewer_id: str = Field(min_length=1, max_length=160)
    comment: str | None = Field(default=None, max_length=2000)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if not settings.mock_mode:
        settings.validate_for_live_mode()

    search_provider, website_reader = build_source_adapters(settings)
    crm_adapter = build_crm_adapter(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # AsyncSqliteSaver is required because the HTTP API calls graph.ainvoke/aget_state.
        async with AsyncSqliteSaver.from_conn_string(settings.sqlite_path) as checkpointer:
            await checkpointer.setup()
            app.state.graph = create_graph(
                settings=settings,
                search_provider=search_provider,
                website_reader=website_reader,
                crm_adapter=crm_adapter,
                checkpointer=checkpointer,
            )
            yield
            if isinstance(crm_adapter, HubSpotCRM):
                await crm_adapter.aclose()

    app = FastAPI(
        title="Lead Research & CRM Loader Agent",
        version="0.1.0",
        description="Evidence-backed company research with explicit human approval before CRM writes.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.crm_adapter = crm_adapter

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": "mock" if settings.mock_mode else "live"}

    @app.post("/runs")
    async def start_run(request_body: LeadRequest, request: Request) -> dict[str, Any]:
        graph = request.app.state.graph
        run_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": run_id}}
        try:
            result = await graph.ainvoke(
                {"request": request_body.model_dump(mode="json"), "run_id": run_id, "events": []},
                config=config,
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Workflow failed: {type(exc).__name__}: {exc}") from exc
        if result.get("__interrupt__"):
            interrupt_value = result["__interrupt__"][0].value
            return {"run_id": run_id, "status": "awaiting_approval", "approval": interrupt_value}
        return {"run_id": run_id, "status": "completed", "report": result.get("report"), "error": result.get("error")}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> dict[str, Any]:
        graph = request.app.state.graph
        snapshot = await graph.aget_state({"configurable": {"thread_id": run_id}})
        if not snapshot or not snapshot.values:
            raise HTTPException(status_code=404, detail="Run not found")
        values = snapshot.values
        if values.get("report"):
            status = "completed"
        elif "approval_gate" in snapshot.next:
            status = "awaiting_approval"
        else:
            status = "running"
        return {
            "run_id": run_id,
            "status": status,
            "request": values.get("request"),
            "approval": values.get("approval_payload") if status == "awaiting_approval" else None,
            "report": values.get("report"),
            "next": list(snapshot.next),
        }

    @app.post("/runs/{run_id}/approval")
    async def resume_with_approval(run_id: str, body: ApprovalBody, request: Request) -> dict[str, Any]:
        graph = request.app.state.graph
        try:
            decision = ApprovalDecision.model_validate(body.model_dump())
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        config = {"configurable": {"thread_id": run_id}}
        snapshot = await graph.aget_state(config)
        if not snapshot or not snapshot.values:
            raise HTTPException(status_code=404, detail="Run not found")
        if "approval_gate" not in snapshot.next:
            raise HTTPException(status_code=409, detail="Run is not waiting for approval")
        try:
            result = await graph.ainvoke(Command(resume=decision.model_dump(mode="json")), config=config)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Could not resume workflow: {type(exc).__name__}: {exc}") from exc
        if result.get("__interrupt__"):
            return {"run_id": run_id, "status": "awaiting_approval", "approval": result["__interrupt__"][0].value}
        return {
            "run_id": run_id,
            "status": "completed",
            "approval_result": result.get("approval"),
            "report": result.get("report"),
            "error": result.get("error"),
        }

    @app.get("/runs/{run_id}/report")
    async def get_report(run_id: str, request: Request) -> dict[str, Any]:
        graph = request.app.state.graph
        snapshot = await graph.aget_state({"configurable": {"thread_id": run_id}})
        if not snapshot or not snapshot.values:
            raise HTTPException(status_code=404, detail="Run not found")
        report = snapshot.values.get("report")
        if not report:
            raise HTTPException(status_code=409, detail="Report is not ready yet; the run may be awaiting approval")
        return report

    @app.get("/mock-crm/companies")
    async def list_mock_companies(request: Request) -> dict[str, Any]:
        adapter = request.app.state.crm_adapter
        if not isinstance(adapter, MockCRM):
            raise HTTPException(status_code=404, detail="Mock CRM is disabled in live mode")
        return {"results": await adapter.list_companies()}

    @app.get("/mock-crm/companies/{record_id}")
    async def get_mock_company(record_id: str, request: Request) -> dict[str, Any]:
        adapter = request.app.state.crm_adapter
        if not isinstance(adapter, MockCRM):
            raise HTTPException(status_code=404, detail="Mock CRM is disabled in live mode")
        record = await adapter.get_company(record_id)
        if not record:
            raise HTTPException(status_code=404, detail="Company record not found")
        return record

    return app


app = create_app()
