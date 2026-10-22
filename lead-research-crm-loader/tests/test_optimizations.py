from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

import pytest
import httpx
from langgraph.checkpoint.memory import InMemorySaver

from lead_crm_loader import graph as graph_module
from lead_crm_loader import sources as sources_module
from lead_crm_loader.call_budget import RunCallBudget, reset_budget, set_budget
from lead_crm_loader.config import Settings
from lead_crm_loader.crm import InMemoryCRM
from lead_crm_loader.models import (
    Candidate,
    CandidateClassification,
    Classification,
    Decision,
    Evidence,
    LeadRequest,
    SearchResult,
    WebsiteSnapshot,
    WebsiteStatus,
)
from lead_crm_loader.qualification import QualificationService
from lead_crm_loader.sources import BrowserbaseSearchProvider, HybridWebsiteReader, PublicWebsiteReader, is_editorial_listicle
from lead_crm_loader.usage import RunUsage, current_usage, reset_usage, set_usage


def test_untrusted_page_text_strips_postgres_unsupported_nul_characters():
    result = SearchResult(
        title="AI\x00 software",
        url="https://example.com/",
        snippet="AI\x00 product",
    )
    candidate = Candidate(
        candidate_id="nul-test",
        request_id="request-test",
        company_name="AI\x00 company",
        domain="example.com",
        website_url="https://example.com/",
        description="Page text\x00 with invalid database character.",
        source_evidence=[Evidence(
            evidence_id="nul-evidence",
            source_url="https://example.com/",
            source_type="company_website",
            excerpt="AI\x00 software product",
        )],
        verification_notes=["A\x00 note"],
    )

    assert "\x00" not in result.title + result.snippet
    assert "\x00" not in candidate.company_name + candidate.description
    assert "\x00" not in candidate.source_evidence[0].excerpt
    assert "\x00" not in candidate.verification_notes[0]


def make_candidate(candidate_id: str, evidence_id: str) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        request_id="request-test",
        company_name=f"Company {candidate_id}",
        domain=f"{candidate_id}.example",
        website_url=f"https://{candidate_id}.example/",
        description="B2B AI software company in Bengaluru.",
        raw_search_snippet="B2B AI software company in Bengaluru.",
        website_status=WebsiteStatus.ACTIVE,
        source_evidence=[Evidence(
            evidence_id=evidence_id,
            source_url=f"https://{candidate_id}.example/",
            source_type="company_website",
            excerpt="B2B AI software company in Bengaluru.",
        )],
    )


def make_request(target: int = 1) -> LeadRequest:
    return LeadRequest(
        request_id="request-test",
        industry="AI software",
        region="Bengaluru",
        qualification_criteria=["B2B AI software company"],
        target_leads=target,
    )


class StubLLM:
    def __init__(self, results: list[CandidateClassification]) -> None:
        self.results = results
        self.calls: list[list[str]] = []

    async def classify_many(self, _request: LeadRequest, candidates: list[Candidate]) -> list[CandidateClassification]:
        self.calls.append([candidate.candidate_id for candidate in candidates])
        return self.results


@pytest.mark.asyncio
async def test_batch_qualification_rejects_cross_candidate_evidence_ids():
    candidate_a = make_candidate("company-a", "evidence-a")
    candidate_b = make_candidate("company-b", "evidence-b")
    fake_llm = StubLLM([
        CandidateClassification(
            candidate_id="company-a", decision=Decision.QUALIFIED, confidence=0.9,
            rationale="Supported.", evidence_ids=["evidence-a"],
        ),
        CandidateClassification(
            candidate_id="company-b", decision=Decision.QUALIFIED, confidence=0.9,
            rationale="Wrong company evidence.", evidence_ids=["evidence-a"],
        ),
    ])
    service = QualificationService(Settings(mock_mode=False, min_confidence=0.7))
    service.llm = fake_llm

    results = await service.classify_many(make_request(), [candidate_a, candidate_b])

    assert fake_llm.calls == [["company-a", "company-b"]]
    assert results["company-a"].decision == Decision.QUALIFIED
    assert results["company-b"].decision == Decision.UNCERTAIN
    assert results["company-b"].evidence_ids == []


@pytest.mark.asyncio
async def test_batch_qualification_marks_omitted_or_duplicate_items_uncertain():
    candidate_a = make_candidate("company-a", "evidence-a")
    candidate_b = make_candidate("company-b", "evidence-b")
    repeated = CandidateClassification(
        candidate_id="company-a", decision=Decision.QUALIFIED, confidence=0.9,
        rationale="Supported.", evidence_ids=["evidence-a"],
    )
    service = QualificationService(Settings(mock_mode=False))
    service.llm = StubLLM([repeated, repeated])

    results = await service.classify_many(make_request(), [candidate_a, candidate_b])

    assert results["company-a"].decision == Decision.UNCERTAIN
    assert results["company-b"].decision == Decision.UNCERTAIN


@pytest.mark.asyncio
async def test_llm_qualification_requires_cited_region_product_and_official_site_evidence():
    request = LeadRequest(
        request_id="request-test",
        industry="AI software",
        region="India",
        qualification_criteria=[
            "Develops AI or machine-learning software products for business customers",
            "Company is based in India",
            "Has an active official company website",
        ],
    )
    candidate = Candidate(
        candidate_id="service-company",
        request_id="request-test",
        company_name="AI Development Services",
        domain="services.example",
        website_url="https://services.example/",
        website_status=WebsiteStatus.ACTIVE,
        description="Custom AI and software development services for business customers.",
        source_evidence=[Evidence(
            evidence_id="service-evidence",
            source_url="https://services.example/",
            source_type="company_website",
            excerpt="Custom AI and software development services for business customers.",
        )],
    )
    service = QualificationService(Settings(mock_mode=False, min_confidence=0.7))
    service.llm = StubLLM([CandidateClassification(
        candidate_id="service-company",
        decision=Decision.QUALIFIED,
        confidence=0.99,
        rationale="The company meets the requested criteria.",
        evidence_ids=["service-evidence"],
    )])

    result = await service.classify_many(request, [candidate])

    assert result["service-company"].decision == Decision.UNCERTAIN
    assert "Location evidence for India" in result["service-company"].missing_criteria
    assert any("product or platform" in item for item in result["service-company"].missing_criteria)


@pytest.mark.asyncio
async def test_product_signal_only_in_search_result_cannot_qualify_company():
    request = LeadRequest(
        request_id="request-test",
        industry="AI software",
        region="India",
        qualification_criteria=["Develops AI software products for business customers"],
    )
    candidate = Candidate(
        candidate_id="snippet-only-product",
        request_id="request-test",
        company_name="AI Services",
        domain="services.example",
        website_url="https://services.example/",
        website_status=WebsiteStatus.ACTIVE,
        description="AI software development services in India.",
        source_evidence=[
            Evidence(
                evidence_id="official-evidence",
                source_url="https://services.example/",
                source_type="company_website",
                excerpt="AI software development services in India for business customers.",
            ),
            Evidence(
                evidence_id="search-evidence",
                source_url="https://search.example/result",
                source_type="web_search_result",
                excerpt="The company builds AI software products and SaaS platforms.",
            ),
        ],
    )
    service = QualificationService(Settings(mock_mode=False, min_confidence=0.7))
    service.llm = StubLLM([CandidateClassification(
        candidate_id="snippet-only-product",
        decision=Decision.QUALIFIED,
        confidence=0.99,
        rationale="The company meets the requested criteria.",
        evidence_ids=["official-evidence", "search-evidence"],
    )])

    result = await service.classify_many(request, [candidate])

    assert result["snippet-only-product"].decision == Decision.UNCERTAIN
    assert any("Official-site evidence" in item for item in result["snippet-only-product"].missing_criteria)


def test_editorial_listicles_on_insights_pages_are_not_company_candidates():
    assert is_editorial_listicle(
        "https://worktual.in/insights/top-ai-native-saas-companies-india",
        "Top 10 AI-Native SaaS Companies in India 2026 | Worktual",
    )
    assert not is_editorial_listicle(
        "https://example.in/products/ai-platform",
        "AI Software Platform for Business | Example",
    )


@pytest.mark.asyncio
async def test_browserbase_search_retries_connect_errors_and_counts_each_call(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"title": "Example AI", "url": "https://example.ai/", "snippet": "Official site."}]}

    class FakeClient:
        def __init__(self, **_kwargs):
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError("temporary connection failure")
            return FakeResponse()

    client = FakeClient()
    monkeypatch.setattr(sources_module.httpx, "AsyncClient", lambda **_kwargs: client)
    usage = RunUsage()
    usage_token = set_usage(usage)
    budget = RunCallBudget(browserbase_limit=3, llm_limit=1)
    budget_token = set_budget(budget)
    try:
        results = await BrowserbaseSearchProvider("test-key").search("India AI vendors")
    finally:
        reset_budget(budget_token)
        reset_usage(usage_token)

    assert len(results) == 1
    assert results[0].title == "Example AI"
    assert client.calls == 2
    assert budget.browserbase_calls == 2
    assert usage.snapshot()["browserbase_search_requests"] == 2


def test_location_research_only_follows_same_site_links():
    assert graph_module._official_location_page_url(
        [
            {"url": "https://company.example/contact", "text": "Contact"},
            {"url": "https://company.example/about-us", "text": "About"},
            {"url": "https://profiles.example/company", "text": "Company profile"},
        ],
        "https://company.example/",
    ) == "https://company.example/about-us"


def test_public_reader_flags_only_renderable_javascript_shells():
    shell = PublicWebsiteReader._parse(
        "https://company.example/", "https://company.example/", 200,
        "<html><body><div id='root'></div><script>window.app={};</script></body></html>", None,
    )
    plain = PublicWebsiteReader._parse(
        "https://company.example/about", "https://company.example/about", 200,
        "<html><body><h1>About</h1><p>Our software business and services.</p></body></html>", None,
    )
    denied = PublicWebsiteReader._parse(
        "https://company.example/login", "https://company.example/login", 403,
        "<html><body><div id='root'></div><script>window.app={};</script></body></html>", None,
    )
    login_shell = PublicWebsiteReader._parse(
        "https://company.example/login", "https://company.example/login", 200,
        "<html><body><div id='root'></div><script>window.app={};</script><p>Authentication required. Please log in.</p></body></html>", None,
    )

    assert shell.needs_browser is True
    assert plain.needs_browser is False
    assert denied.needs_browser is False
    assert denied.outcome == "unknown"
    assert login_shell.needs_browser is False


class StubReader:
    def __init__(self, snapshot: WebsiteSnapshot) -> None:
        self.snapshot = snapshot
        self.fetch_calls = 0
        self.verify_calls = 0

    async def fetch(self, _url: str) -> WebsiteSnapshot:
        self.fetch_calls += 1
        return self.snapshot

    async def verify(self, _url: str) -> WebsiteSnapshot:
        self.verify_calls += 1
        return self.snapshot


@pytest.mark.asyncio
async def test_hybrid_reader_uses_browserbase_only_for_reachable_js_shells():
    url = "https://company.example/"
    direct = StubReader(WebsiteSnapshot(requested_url=url, outcome="reachable", needs_browser=True, text=""))
    rendered = StubReader(WebsiteSnapshot(requested_url=url, outcome="reachable", text="Rendered product details"))
    reader = HybridWebsiteReader(direct, rendered)

    fetched = await reader.fetch(url)
    verified = await reader.verify(url)

    assert fetched.text == "Rendered product details"
    assert rendered.fetch_calls == 1
    assert direct.verify_calls == 1
    assert rendered.verify_calls == 0

    denied = StubReader(WebsiteSnapshot(requested_url=url, status_code=403, outcome="unknown", needs_browser=False))
    denied_fallback = StubReader(WebsiteSnapshot(requested_url=url, outcome="reachable", text="Should not be used"))
    denied_reader = HybridWebsiteReader(denied, denied_fallback)
    assert (await denied_reader.fetch(url)).outcome == "unknown"
    assert denied_fallback.fetch_calls == 0


@pytest.mark.asyncio
async def test_hybrid_reader_renders_detected_js_shell_without_browserbase_raw_fetch():
    url = "https://company.example/"
    direct = StubReader(WebsiteSnapshot(requested_url=url, outcome="reachable", needs_browser=True))

    class RenderPreferredReader(StubReader):
        def __init__(self, snapshot: WebsiteSnapshot) -> None:
            super().__init__(snapshot)
            self.render_calls = 0

        async def render(self, _url: str) -> WebsiteSnapshot:
            self.render_calls += 1
            return self.snapshot

    browserbase = RenderPreferredReader(
        WebsiteSnapshot(requested_url=url, outcome="reachable", text="Rendered product details")
    )
    reader = HybridWebsiteReader(direct, browserbase)

    result = await reader.fetch(url)

    assert result.text == "Rendered product details"
    assert browserbase.render_calls == 1
    assert browserbase.fetch_calls == 0


@pytest.mark.asyncio
async def test_hybrid_reader_uses_browserbase_fetch_when_direct_network_is_unavailable():
    url = "https://company.example/"
    direct = StubReader(WebsiteSnapshot(
        requested_url=url,
        outcome="unknown",
        error="DNS resolution failed: no address associated with hostname",
    ))
    browserbase = StubReader(WebsiteSnapshot(
        requested_url=url,
        status_code=200,
        outcome="reachable",
        text="B2B AI company based in India",
    ))
    reader = HybridWebsiteReader(direct, browserbase)

    fetched = await reader.fetch(url)
    verified = await reader.verify(url)

    assert fetched.outcome == "reachable"
    assert verified.outcome == "reachable"
    assert browserbase.fetch_calls == 1
    assert browserbase.verify_calls == 1


class TwoRoundSearch:
    def __init__(self, result_factory: Callable[[str], SearchResult]) -> None:
        self.calls = 0
        self.result_factory = result_factory
        self.targeted_queries: list[str] = []

    async def search(self, query: str, _max_results: int = 20) -> list[SearchResult]:
        self.calls += 1
        if query.startswith('"'):
            self.targeted_queries.append(query)
            return [self.result_factory("https://company-a.example/about")]
        if self.calls in {1, 3}:
            suffix = "a" if self.calls == 1 else "b"
            return [self.result_factory(f"https://company-{suffix}.example/")]
        return []


class SingleResultSearch:
    def __init__(self, result: SearchResult) -> None:
        self.result = result

    async def search(self, _query: str, _max_results: int = 20) -> list[SearchResult]:
        return [self.result]


class ActiveReader:
    async def fetch(self, url: str) -> WebsiteSnapshot:
        company = (urlparse(url).hostname or "Company").split(".")[0].replace("-", " ").title()
        return WebsiteSnapshot(
            requested_url=url,
            final_url=url,
            status_code=200,
            title=f"{company} AI Software",
            h1=f"{company} AI Software",
            text="B2B AI software company serving customers in Bengaluru.",
            outcome="reachable",
        )

    async def verify(self, url: str) -> WebsiteSnapshot:
        return await self.fetch(url)


class RecordingQualification:
    def __init__(self, decision: Decision, confidence: float) -> None:
        self.decision = decision
        self.confidence = confidence
        self.calls: list[list[str]] = []
        self.llm = None

    async def classify_many(self, _request: LeadRequest, candidates: list[Candidate]) -> dict[str, Classification]:
        self.calls.append([candidate.candidate_id for candidate in candidates])
        usage = current_usage()
        if usage is not None:
            usage.increment("llm_batch_calls")
            usage.increment("llm_http_attempts")
        return {
            candidate.candidate_id: Classification(
                decision=self.decision,
                confidence=self.confidence,
                rationale="Stubbed classification.",
                matched_criteria=["B2B AI software company"],
                evidence_ids=[evidence.evidence_id for evidence in candidate.source_evidence[:1]],
            )
            for candidate in candidates
        }


def search_result(url: str) -> SearchResult:
    return SearchResult(
        title=f"Company {url}",
        url=url,
        snippet="AI software company in Bengaluru; B2B enterprise products.",
        source_type="web_search_result",
    )


@pytest.mark.asyncio
async def test_graph_batches_new_candidates_without_reclassifying_prior_round(monkeypatch: pytest.MonkeyPatch):
    qualification = RecordingQualification(Decision.QUALIFIED, 0.9)
    monkeypatch.setattr(graph_module, "QualificationService", lambda _settings: qualification)
    search = TwoRoundSearch(search_result)
    settings = Settings(
        mock_mode=True, database_url="memory://", number_of_leads=2,
        max_candidates=10, max_search_rounds=2, llm_batch_size=25,
    )
    graph = graph_module.create_graph(
        settings=settings, search_provider=search, website_reader=ActiveReader(),
        crm_adapter=InMemoryCRM(), checkpointer=InMemorySaver(),
    )
    tracker = RunUsage()
    usage_token = set_usage(tracker)
    try:
        result = await graph.ainvoke(
            {"request": make_request(target=2).model_dump(mode="json"), "run_id": "batch-run", "events": []},
            config={"configurable": {"thread_id": "batch-run"}},
        )
        snapshot = await graph.aget_state({"configurable": {"thread_id": "batch-run"}})
    finally:
        reset_usage(usage_token)

    assert result.get("__interrupt__")
    assert len(qualification.calls) == 2
    assert all(len(batch) == 1 for batch in qualification.calls), f"Unexpected reclassification batches: {qualification.calls}"
    assert len({candidate_id for batch in qualification.calls for candidate_id in batch}) == 2
    assert snapshot.values["usage"]["llm_batch_calls"] == 2


@pytest.mark.asyncio
async def test_graph_caps_targeted_company_searches_and_reports_usage(monkeypatch: pytest.MonkeyPatch):
    qualification = RecordingQualification(Decision.UNCERTAIN, 0.6)
    monkeypatch.setattr(graph_module, "QualificationService", lambda _settings: qualification)
    search = TwoRoundSearch(search_result)
    settings = Settings(
        mock_mode=True, database_url="memory://", number_of_leads=1,
        max_candidates=10, max_search_rounds=1, max_targeted_searches_per_run=1,
    )
    graph = graph_module.create_graph(
        settings=settings, search_provider=search, website_reader=ActiveReader(),
        crm_adapter=InMemoryCRM(), checkpointer=InMemorySaver(),
    )
    usage_token = set_usage(RunUsage())
    try:
        result = await graph.ainvoke(
            {"request": make_request().model_dump(mode="json"), "run_id": "targeted-run", "events": []},
            config={"configurable": {"thread_id": "targeted-run"}},
        )
    finally:
        reset_usage(usage_token)

    assert result["report"]["usage"]["targeted_company_searches"] == 1
    assert len(search.targeted_queries) == 1
    assert result["report"]["usage"]["llm_batch_calls"] == 2


@pytest.mark.asyncio
async def test_irrelevant_result_defers_fetch_without_name_error():
    reader = StubReader(WebsiteSnapshot(
        requested_url="https://farm-machinery.example/",
        outcome="reachable",
        text="This page should not be fetched during AI-company triage.",
    ))
    graph = graph_module.create_graph(
        settings=Settings(mock_mode=True, database_url="memory://", number_of_leads=1, max_candidates=2, max_search_rounds=1),
        search_provider=SingleResultSearch(SearchResult(
            title="Sustainable Farm Machinery",
            url="https://farm-machinery.example/",
            snippet="Agriculture equipment for sustainable farms in India.",
            source_type="web_search_result",
        )),
        website_reader=reader,
        crm_adapter=InMemoryCRM(),
        checkpointer=InMemorySaver(),
    )

    result = await graph.ainvoke(
        {"request": make_request(target=1).model_dump(mode="json"), "run_id": "irrelevant-result-run", "events": []},
        config={"configurable": {"thread_id": "irrelevant-result-run"}},
    )

    assert reader.fetch_calls == 0
    assert result["report"]["discovered_candidates"] == 1
