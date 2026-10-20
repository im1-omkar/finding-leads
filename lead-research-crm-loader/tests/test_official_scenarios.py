from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from lead_crm_loader.config import Settings
from lead_crm_loader.crm import MockCRM
from lead_crm_loader.graph import create_graph
from lead_crm_loader.models import LeadRequest
from lead_crm_loader.sources import MockSearchProvider, MockWebsiteReader


@pytest.fixture
async def full_run(tmp_path: Path):
    db_path = str(tmp_path / "agent-test.sqlite3")
    settings = Settings(mock_mode=True, sqlite_path=db_path, max_candidates=40, max_concurrency=3)
    search = MockSearchProvider()
    reader = MockWebsiteReader()
    crm = MockCRM(db_path)
    graph = create_graph(
        settings=settings,
        search_provider=search,
        website_reader=reader,
        crm_adapter=crm,
        checkpointer=InMemorySaver(),
    )
    request = LeadRequest(
        request_id="test_req_001",
        industry="AI software",
        region="Bengaluru",
        qualification_criteria=["B2B AI software company"],
        target_leads=50,
    )
    config = {"configurable": {"thread_id": "test-run-001"}}
    first = await graph.ainvoke(
        {"request": request.model_dump(mode="json"), "run_id": "test-run-001", "events": []},
        config=config,
    )
    assert first.get("__interrupt__"), "The workflow must pause for human approval before CRM writes."
    interrupt_value = first["__interrupt__"][0].value
    ids = interrupt_value["changes"]
    approved_ids = [item["candidate_id"] for item in ids]
    final = await graph.ainvoke(
        Command(resume={
            "approved": True,
            "proposal_hash": interrupt_value["proposal_hash"],
            "approved_candidate_ids": approved_ids,
            "reviewer_id": "test-reviewer",
            "comment": "Approve all mock-qualified active companies for the integration test.",
        }),
        config=config,
    )
    return final


@pytest.mark.asyncio
async def test_tc1_target_niche_has_source_per_unique_lead(full_run):
    report = full_run["report"]
    candidates = report["candidates"]
    canonical_qualified = [c for c in candidates if c["decision"] == "qualified" and c["website_status"] == "active"]
    assert canonical_qualified
    assert all(c["source_evidence"] for c in canonical_qualified)
    assert all(any(e["source_url"] for e in c["source_evidence"]) for c in canonical_qualified)
    domains = [c["domain"] for c in canonical_qualified]
    assert len(domains) == len(set(domains))


@pytest.mark.asyncio
async def test_tc2_two_domains_of_one_company_merge_to_one_canonical_lead(full_run):
    candidates = full_run["report"]["candidates"]
    acme_candidates = [c for c in candidates if "acme ai" in c["company_name"].lower()]
    assert len(acme_candidates) >= 2
    assert sum(c["decision"] == "qualified" for c in acme_candidates) == 1
    assert sum(c["decision"] == "duplicate" for c in acme_candidates) >= 1
    canonical = next(c for c in acme_candidates if c["decision"] == "qualified")
    assert len(canonical["source_evidence"]) >= 2


@pytest.mark.asyncio
async def test_tc3_non_resolving_website_is_marked_inactive(full_run):
    candidates = full_run["report"]["candidates"]
    dead = next(c for c in candidates if "deadstack-ai.invalid" in c["domain"])
    assert dead["website_status"] == "inactive"
    assert dead["import_status"] == "skipped_inactive"
    assert any("availability" in note.lower() for note in dead["verification_notes"])


@pytest.mark.asyncio
async def test_tc4_login_gated_contact_page_is_skipped(full_run):
    candidates = full_run["report"]["candidates"]
    gated = next(c for c in candidates if "gated-labs.example" in c["domain"])
    assert gated["contact_access"] == "skipped_requires_login"
    # The adapter fetches only the public homepage; contact links are inspected but never traversed.
    assert gated["website_url"].rstrip("/").endswith("gated-labs.example")


@pytest.mark.asyncio
async def test_approval_is_mandatory_before_mock_crm_writes(tmp_path: Path):
    db_path = str(tmp_path / "approval-test.sqlite3")
    settings = Settings(mock_mode=True, sqlite_path=db_path, max_candidates=40, max_concurrency=3)
    crm = MockCRM(db_path)
    graph = create_graph(
        settings=settings,
        search_provider=MockSearchProvider(),
        website_reader=MockWebsiteReader(),
        crm_adapter=crm,
        checkpointer=InMemorySaver(),
    )
    request = LeadRequest(
        request_id="approval_req_001",
        industry="AI software",
        region="Bengaluru",
        qualification_criteria=["B2B AI software company"],
        target_leads=50,
    )
    config = {"configurable": {"thread_id": "approval-run"}}
    first = await graph.ainvoke({"request": request.model_dump(mode="json"), "run_id": "approval-run", "events": []}, config=config)
    assert first.get("__interrupt__")
    assert await crm.list_companies() == []
    proposal = first["__interrupt__"][0].value
    final = await graph.ainvoke(
        Command(resume={
            "approved": False,
            "proposal_hash": proposal["proposal_hash"],
            "approved_candidate_ids": [],
            "reviewer_id": "test-reviewer",
            "comment": "Reject for test.",
        }),
        config=config,
    )
    assert final["report"]["imported"] == 0
    assert await crm.list_companies() == []
