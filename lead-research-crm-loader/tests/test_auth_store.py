from datetime import datetime, timedelta, timezone

import pytest

from lead_crm_loader.run_store import InMemoryRunStore


@pytest.mark.asyncio
async def test_run_history_is_private_and_paginated() -> None:
    store = InMemoryRunStore()
    await store.create_run("run-a", {"request_id": "request-a"}, "user-a")
    await store.create_run("run-b", {"request_id": "request-b"}, "user-b")

    assert await store.owns_run("run-a", "user-a")
    assert not await store.owns_run("run-a", "user-b")
    rows, total = await store.list_runs("user-a", limit=1, offset=0)
    assert total == 1
    assert [row["run_id"] for row in rows] == ["run-a"]


@pytest.mark.asyncio
async def test_session_expiry_and_auth_tokens_are_single_use() -> None:
    store = InMemoryRunStore()
    user = await store.create_user("user-a", "person@example.com", "argon-hash")
    assert user is not None

    await store.create_session("user-a", "session-hash", datetime.now(timezone.utc) + timedelta(seconds=30))
    assert await store.get_user_for_session("session-hash")
    await store.delete_session("session-hash")
    assert await store.get_user_for_session("session-hash") is None
    await store.create_session("user-a", "expired-session", datetime.now(timezone.utc) - timedelta(seconds=1))
    assert await store.get_user_for_session("expired-session") is None

    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    await store.create_auth_token("user-a", "verify-hash", "verify", expires)
    assert await store.consume_auth_token("verify-hash", "verify") == "user-a"
    assert await store.consume_auth_token("verify-hash", "verify") is None
