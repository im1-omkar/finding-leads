from __future__ import annotations

from typing import Any

from langgraph.types import Command


async def execute_run_job(job: dict[str, Any], graph: Any, store: Any) -> None:
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
