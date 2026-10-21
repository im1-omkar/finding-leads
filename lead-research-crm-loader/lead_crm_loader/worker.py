from __future__ import annotations

import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from .config import Settings
from .crm import PostgresCRM, build_crm_adapter
from .execution import execute_run_job
from .graph import create_graph
from .run_store import PostgresRunStore
from .sources import build_source_adapters


async def _heartbeat(store: PostgresRunStore, job_id: int) -> None:
    while True:
        await asyncio.sleep(20)
        await store.heartbeat_job(job_id)


async def run_worker() -> None:
    settings = Settings.from_env()
    if not settings.database_url.startswith(("postgres://", "postgresql://")):
        raise RuntimeError("The background worker requires a PostgreSQL DATABASE_URL")
    if not settings.mock_mode:
        settings.validate_for_live_mode()

    run_store = PostgresRunStore(settings.database_url)
    await run_store.setup()
    await run_store.recover_stale_jobs()
    crm = build_crm_adapter(settings)
    if isinstance(crm, PostgresCRM):
        await crm.setup()
    search, reader = build_source_adapters(settings)

    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as checkpointer:
        await checkpointer.setup()
        graph = create_graph(
            settings=settings,
            search_provider=search,
            website_reader=reader,
            crm_adapter=crm,
            checkpointer=checkpointer,
        )
        try:
            while True:
                job = await run_store.claim_job()
                if job is None:
                    await asyncio.sleep(0.5)
                    continue
                heartbeat = asyncio.create_task(_heartbeat(run_store, int(job["job_id"])))
                try:
                    await execute_run_job(job, graph, run_store, settings)
                    await run_store.finish_job(int(job["job_id"]))
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    await run_store.set_run(str(job["run_id"]), status="failed", error=message)
                    await run_store.append_event(
                        str(job["run_id"]),
                        {"node": "worker", "status": "failed", "message": "Run failed; see error details.", "error": message},
                    )
                    await run_store.finish_job(int(job["job_id"]))
                finally:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass
        finally:
            await crm.aclose()
            await run_store.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
