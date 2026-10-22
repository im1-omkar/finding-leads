from __future__ import annotations

from typing import Any

from langgraph.types import Command
from .call_budget import RunCallBudget, reset_budget, set_budget
from .config import Settings
from .usage import RunUsage, reset_usage, set_usage


async def execute_run_job(job: dict[str, Any], graph: Any, store: Any, settings: Settings | None = None) -> None:
    run_id = str(job["run_id"])
    config = {"configurable": {"thread_id": run_id}}
    await store.set_run(run_id, status="running", error=None)
    await store.append_event(run_id, {"node": "worker", "status": "running", "message": "Research is in progress."})

    if job["job_type"] == "start":
        payload = job["payload"]
        graph_input: Any = {
            "request": payload["request"],
            "run_id": run_id,
            "events": [],
        }
    else:
        graph_input = Command(resume=job["payload"])

    request = job.get("payload", {}).get("request", {})
    budget = None
    if job["job_type"] == "start" and request.get("test_mode"):
        settings = settings or Settings.from_env()
        budget = RunCallBudget(
            browserbase_limit=settings.test_mode_browserbase_call_limit,
            llm_limit=settings.test_mode_llm_call_limit,
        )
    budget_token = set_budget(budget)
    usage_token = set_usage(RunUsage())
    try:
        async for update in graph.astream(graph_input, config=config, stream_mode="updates"):
            for node_update in update.values():
                if not isinstance(node_update, dict):
                    continue
                for event in node_update.get("events", []):
                    await store.append_event(run_id, event)
                if "candidates" in node_update:
                    await store.set_run(run_id, results=node_update["candidates"])
                if "approval_payload" in node_update:
                    await store.set_run(run_id, approval=node_update["approval_payload"])
        if budget is not None:
            await store.append_event(run_id, {
                "node": "test_mode_budget",
                "status": "ok",
                "message": (
                    f"Test mode used {budget.browserbase_calls}/{budget.browserbase_limit} Browserbase requests "
                    f"and {budget.llm_calls}/{budget.llm_limit} LLM calls. Rule-based qualification takes over when the LLM cap is reached."
                ),
            })
    finally:
        reset_budget(budget_token)
        reset_usage(usage_token)

    snapshot = await graph.aget_state(config)
    values = snapshot.values or {}
    candidates = values.get("candidates", [])
    if "approval_gate" in snapshot.next:
        approval = values.get("approval_payload")
        await store.set_run(run_id, status="awaiting_approval", approval=approval, results=candidates)
        await store.append_event(
            run_id,
            {"node": "approval_gate", "status": "awaiting_approval", "message": "Review the lead proposal and approve selected records."},
        )
        return

    report = values.get("report")
    if report:
        await store.set_run(run_id, status="completed", report=report, results=candidates, approval=values.get("approval_payload"))
        await store.append_event(run_id, {"node": "complete", "status": "completed", "message": "Research run completed."})
    else:
        error = values.get("error") or "The workflow ended without a report."
        await store.set_run(run_id, status="failed", error=error, results=candidates)
        await store.append_event(run_id, {"node": "complete", "status": "failed", "message": error})
