from __future__ import annotations

import asyncio
import hashlib
import json
import operator
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypedDict
from urllib.parse import urlparse

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .config import Settings
from .call_budget import current_budget
from .crm import CRMAdapter, build_crm_adapter
from .deduplication import deduplicate_candidates
from .models import (
    AgentReport,
    ApprovalDecision,
    Candidate,
    ContactAccess,
    Decision,
    ImportStatus,
    LeadRequest,
    RunUsageSummary,
    WebsiteSnapshot,
    WebsiteStatus,
)
from .qualification import QualificationService
from .sources import (
    SearchProvider,
    WebsiteReader,
    build_source_adapters,
    canonical_url,
    evidence_from_text,
    find_official_website_link,
    is_business_directory,
    is_editorial_listicle,
    normalized_domain,
    stable_id,
    site_root,
)
from .usage import current_usage, merge_usage, usage_delta


class AgentState(TypedDict, total=False):
    request: dict[str, Any]
    run_id: str
    search_results: list[dict[str, Any]]
    seen_urls: list[str]
    search_round: int
    targeted_searches_used: int
    usage: Annotated[dict[str, int], merge_usage]
    new_results_count: int
    unprocessed_urls: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    proposal: dict[str, Any]
    proposal_hash: str
    approval_payload: dict[str, Any]
    approval: dict[str, Any]
    crm_results: list[dict[str, Any]]
    report: dict[str, Any]
    error: str
    events: Annotated[list[dict[str, Any]], operator.add]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(node: str, status: str, message: str, **details: Any) -> dict[str, Any]:
    return {"at": _now(), "node": node, "status": status, "message": message, **details}


def _candidate_objects(state: AgentState) -> list[Candidate]:
    return [Candidate.model_validate(value) for value in state.get("candidates", [])]


def _candidate_dicts(candidates: list[Candidate]) -> list[dict[str, Any]]:
    return [candidate.model_dump(mode="json") for candidate in candidates]


def _parse_employee_count(text: str) -> int | None:
    # Check ranges before single values so "50-100 employees" is not misread as 100.
    range_match = re.search(r"\b(\d{1,5})\s*[-–]\s*(\d{1,5})\s+employees\b", text, re.IGNORECASE)
    if range_match:
        return (int(range_match.group(1)) + int(range_match.group(2))) // 2
    patterns = [r"\b(\d{1,6})\s+employees\b", r"\bemployees\s*[:\-]?\s*(\d{1,6})\b"]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _clean_company_name(title: str, fallback: str) -> str:
    value = (title or fallback or "Unknown company").strip()
    value = re.split(r"\s+[|–—]\s+|\s+-\s+", value, maxsplit=1)[0]
    value = re.sub(r"\s+(official website|company profile|business directory)$", "", value, flags=re.IGNORECASE)
    return value.strip() or fallback or "Unknown company"


def _proposal_digest(candidate_ids: list[str], candidates: list[Candidate]) -> str:
    by_id = {c.candidate_id: c for c in candidates}
    content = []
    for candidate_id in sorted(candidate_ids):
        candidate = by_id[candidate_id]
        content.append({
            "candidate_id": candidate.candidate_id,
            "name": candidate.legal_name or candidate.company_name,
            "domain": candidate.domain,
            "website_url": candidate.website_url,
            "rationale": candidate.rationale,
            "evidence_ids": sorted(candidate.decision_evidence_ids or [e.evidence_id for e in candidate.source_evidence]),
        })
    serialized = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _candidate_evidence_hash(candidate: Candidate) -> str:
    content = {
        "description": " ".join(candidate.description.split()),
        "raw_search_snippet": " ".join(candidate.raw_search_snippet.split()),
        "employee_count": candidate.employee_count,
        "pre_qualified": candidate.pre_qualified,
        "evidence": [
            {
                "evidence_id": evidence.evidence_id,
                "content_hash": evidence.content_hash,
                "excerpt": evidence.excerpt,
            }
            for evidence in candidate.source_evidence
        ],
    }
    serialized = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _official_location_page_url(links: list[dict[str, str]], official_url: str) -> str | None:
    """Choose one same-site About, location, or contact page for location verification."""
    official_domain = normalized_domain(official_url)
    choices: list[tuple[int, str]] = []
    for link in links:
        url = link.get("url", "")
        if not url.startswith(("http://", "https://")) or normalized_domain(url) != official_domain:
            continue
        path = urlparse(url).path.lower()
        label = link.get("text", "").lower()
        combined = f"{path} {label}"
        score = max(
            (value for marker, value in (("location", 4), ("about", 3), ("contact", 2), ("our story", 1), ("who we are", 1)) if marker in combined),
            default=0,
        )
        if score:
            choices.append((score, url))
    choices.sort(key=lambda item: (-item[0], item[1]))
    return choices[0][1] if choices else None


def create_graph(
    settings: Settings | None = None,
    search_provider: SearchProvider | None = None,
    website_reader: WebsiteReader | None = None,
    crm_adapter: CRMAdapter | None = None,
    checkpointer: Any | None = None,
):
    settings = settings or Settings.from_env()
    if search_provider is None or website_reader is None:
        default_search, default_reader = build_source_adapters(settings)
        search_provider = search_provider or default_search
        website_reader = website_reader or default_reader
    crm_adapter = crm_adapter or build_crm_adapter(settings)
    classifier = QualificationService(settings)

    def search_round_limit(request: LeadRequest) -> int:
        return min(settings.max_search_rounds, 1) if request.test_mode else settings.max_search_rounds

    async def validate_input(state: AgentState) -> dict[str, Any]:
        request = LeadRequest.model_validate(state["request"]).model_copy(
            update={"target_leads": settings.number_of_leads}
        )
        run_id = state.get("run_id") or str(uuid.uuid4())
        return {
            "request": request.model_dump(mode="json"),
            "run_id": run_id,
            "events": [_event("validate_input", "ok", "Validated research request.", request_id=request.request_id, target=request.target_leads)],
        }

    async def discover_candidates(state: AgentState) -> dict[str, Any]:
        request = LeadRequest.model_validate(state["request"])
        round_number = int(state.get("search_round", 0)) + 1
        existing_urls = set(state.get("seen_urls", []))
        candidate_limit = min(settings.max_candidates, settings.test_mode_candidate_url_limit) if request.test_mode else settings.max_candidates
        remaining_budget = max(0, candidate_limit - len(existing_urls))
        size_text = ""
        if request.company_size.min_employees or request.company_size.max_employees:
            size_text = f" companies with {request.company_size.min_employees or 1}-{request.company_size.max_employees or 'many'} employees"
        criteria_phrase = " ".join(request.qualification_criteria[:3])
        size_suffix = f" {size_text.strip()}" if size_text else ""
        query_rounds = [
            [
                f"{request.region} B2B {request.industry} software product companies official websites",
                f"{request.region} {request.industry} SaaS startups vendors company websites",
            ],
            [
                f"{request.region} {request.industry} {criteria_phrase} company products",
                f"{request.industry} companies serving business customers in {request.region} official website",
            ],
            [
                f"{request.region} {request.industry} enterprise software companies product website",
                f"{request.region} {request.industry} software vendors platforms company official",
            ],
            [
                f"{request.region} {request.industry} platform providers product websites",
                f"{request.industry} B2B product companies in {request.region} company website",
            ],
            [
                f"{request.region} {request.industry} software platforms for businesses product websites",
                f"{request.region} {request.industry} business software providers official",
            ],
            [
                f"{request.region} {request.industry} software solutions providers for business official",
                f"{request.region} {request.industry} SaaS company products platform",
            ],
            [
                f"{request.region} {request.industry} startups product company official website",
                f"{request.region} {request.industry} software vendors customer solutions",
            ],
            [
                f"{request.region} {request.industry} business software platforms vendor website",
                f"{request.region} {request.industry} technology companies products",
            ],
            [
                f"{request.region} {request.industry} company product customers official site",
                f"{request.region} {request.industry} software firms enterprise solution provider",
            ],
            [
                f"{request.region} {request.industry} product-led B2B software companies official website",
                f"{request.region} {request.industry} company platform features customers",
            ],
        ]
        queries = [f"{query}{size_suffix}" for query in query_rounds[(round_number - 1) % len(query_rounds)]]
        if remaining_budget <= 0:
            return {
                "search_results": [],
                "seen_urls": sorted(existing_urls),
                "search_round": round_number,
                "new_results_count": 0,
                "events": [_event("discover_candidates", "budget_exhausted", "Candidate research budget was exhausted.", search_round=round_number)],
            }
        per_query = min(20, max(5, (remaining_budget + len(queries) - 1) // len(queries)))
        # One flaky query should not abort the entire run. Preserve partial results and record
        # provider failures so the final report can explain why the target may not be reached.
        batch_results = await asyncio.gather(
            *(search_provider.search(query, per_query) for query in queries),
            return_exceptions=True,
        )
        failed_queries = [type(item).__name__ for item in batch_results if isinstance(item, BaseException)]
        batches = [item for item in batch_results if not isinstance(item, BaseException)]
        seen = set(existing_urls)
        found = []
        editorial_filtered = 0
        for batch in batches:
            for item in batch:
                try:
                    normalized = canonical_url(item.url)
                    domain = normalized_domain(item.url)
                    if not domain or normalized in seen:
                        continue
                    seen.add(normalized)
                    if is_editorial_listicle(item.url, item.title, item.snippet):
                        editorial_filtered += 1
                        continue
                    found.append(item.model_copy(update={"url": item.url.strip()}))
                except Exception:
                    continue
                if len(found) >= remaining_budget:
                    break
            if len(found) >= remaining_budget:
                break
        return {
            "search_results": [item.model_dump(mode="json") for item in found],
            "unprocessed_urls": [item.model_dump(mode="json") for item in found],
            "seen_urls": sorted(seen),
            "search_round": round_number,
            "new_results_count": len(found),
            "events": [_event(
                "discover_candidates",
                "partial_failure" if failed_queries else "ok",
                f"Research round {round_number}: discovered {len(found)} new candidate URLs; {len(failed_queries)} search query/queries failed.",
                search_round=round_number,
                queries=queries,
                remaining_budget=remaining_budget,
                editorial_pages_filtered=editorial_filtered,
                failed_query_errors=failed_queries,
            )],
        }

    async def research_candidates(state: AgentState) -> dict[str, Any]:
        request = LeadRequest.model_validate(state["request"])
        # The shift-left stages have already rejected known CRM domains and dead hosts.
        search_results = state.get("unprocessed_urls", state.get("search_results", []))
        semaphore = asyncio.Semaphore(settings.max_concurrency)

        async def research(raw: dict[str, Any]) -> Candidate:
            source_url = raw["url"]
            raw_title = raw.get("title", normalized_domain(source_url))
            raw_snippet = raw.get("snippet", "")
            preliminary_directory = is_business_directory(source_url, raw_title, raw_snippet)
            search_text = f"{raw_title} {raw_snippet}".lower()
            industry_signals = {"ai", "ml", "machine", "learning", "artificial", "intelligence", "software", "saas"}
            has_industry_signal = bool(industry_signals.intersection(re.findall(r"[a-z0-9]+", search_text)))
            if search_text.strip() and not has_industry_signal and not preliminary_directory:
                source_snapshot = WebsiteSnapshot(
                    requested_url=source_url,
                    final_url=source_url,
                    outcome="unknown",
                    error="Page fetch deferred because the search result did not match the requested industry.",
                )
            else:
                async with semaphore:
                    source_snapshot = await website_reader.fetch(source_url)
            source_title = source_snapshot.title or source_snapshot.h1
            title = _clean_company_name(
                source_title if source_snapshot.outcome == "reachable" else "",
                raw.get("title", normalized_domain(source_url)),
            )
            directory_source = preliminary_directory or is_business_directory(
                source_url,
                title=raw.get("title", ""),
                snippet=raw.get("snippet", ""),
                page_text=source_snapshot.text,
            )
            official_url: str | None = None
            company_snapshot = source_snapshot
            if directory_source:
                # Directory content is evidence, not the company's website. Only use an explicit
                # outbound link labelled/recognizable as a website; never guess domains from names.
                official_url = find_official_website_link(source_snapshot, source_url, title)
                if official_url:
                    async with semaphore:
                        company_snapshot = await website_reader.fetch(official_url)
                    if company_snapshot.outcome == "reachable":
                        official_title = company_snapshot.title or company_snapshot.h1
                        if official_title:
                            title = _clean_company_name(official_title, title)
                else:
                    company_snapshot = None

            evidence = [evidence_from_text(
                source_url,
                raw.get("source_type", "web_search_result"),
                f"{raw.get('title', title)}. {raw.get('snippet', '')}",
            )]
            company_text = ""
            contact_access = ContactAccess.UNKNOWN
            if company_snapshot is not None:
                company_text = " ".join([company_snapshot.meta_description, company_snapshot.text]).strip()
                if company_snapshot.text:
                    evidence.append(evidence_from_text(company_snapshot.final_url or official_url or source_url, "company_website", company_text[:2500]))
                contact_access = company_snapshot.contact_access
                contact_snapshot = None
                if contact_access != ContactAccess.SKIPPED_REQUIRES_LOGIN:
                    contact_links = [
                        link for link in company_snapshot.links
                        if "contact" in f"{link.get('text', '')} {link.get('url', '')}".lower()
                    ]
                    if contact_links:
                        # Make only one ordinary unauthenticated request to the public contact URL.
                        # If a login barrier is observed, stop: no auth, OTP/CAPTCHA, or bypass attempts.
                        contact_url = contact_links[0]["url"]
                        async with semaphore:
                            contact_snapshot = await website_reader.fetch(contact_url)
                        final_path = urlparse(contact_snapshot.final_url or contact_url).path.lower()
                        if (
                            contact_snapshot.status_code in {401, 403}
                            or contact_snapshot.contact_access == ContactAccess.SKIPPED_REQUIRES_LOGIN
                            or any(marker in final_path for marker in ["/login", "/signin", "/sign-in", "/auth"])
                        ):
                            contact_access = ContactAccess.SKIPPED_REQUIRES_LOGIN
                        elif contact_snapshot.outcome == "reachable":
                            contact_access = ContactAccess.PUBLIC
                        else:
                            contact_access = ContactAccess.UNKNOWN

                # The homepage often omits the company's location. Reuse a public contact
                # page when it states the requested region; otherwise try one same-site
                # About/location page. This is a direct URL fetch, not another search.
                location_page = None
                if request.region.lower() not in company_text.lower():
                    if contact_snapshot and contact_snapshot.outcome == "reachable" and request.region.lower() in (
                        contact_snapshot.meta_description + " " + contact_snapshot.text
                    ).lower():
                        location_page = contact_snapshot
                    else:
                        about_url = _official_location_page_url(company_snapshot.links, company_snapshot.final_url or official_url or source_url)
                        if about_url and (not contact_snapshot or about_url != contact_url):
                            async with semaphore:
                                about_snapshot = await website_reader.fetch(about_url)
                            if about_snapshot.outcome == "reachable" and request.region.lower() in (
                                about_snapshot.meta_description + " " + about_snapshot.text
                            ).lower():
                                location_page = about_snapshot
                if location_page:
                    location_text = " ".join([location_page.meta_description, location_page.text]).strip()
                    company_text = " ".join([company_text, location_text]).strip()
                    evidence.append(evidence_from_text(
                        location_page.final_url or location_page.requested_url,
                        "company_website",
                        location_text[:2500],
                    ))

            # Keep official proof in the first model-input evidence slots; the LLM is
            # given a bounded sample of three records per company.
            if source_snapshot.text:
                source_evidence = evidence_from_text(source_snapshot.final_url or source_url, "source_page", source_snapshot.text[:2500])
                evidence.append(source_evidence)

            combined = " ".join([raw_snippet, source_snapshot.text, company_text])
            source_for_domain = official_url or source_url
            if directory_source and not official_url:
                website_url = ""
                domain = ""
                site_status = WebsiteStatus.UNKNOWN
                notes = ["A company website could not be established from an explicit public directory link."]
            else:
                website_url = site_root((company_snapshot.final_url if company_snapshot else None) or source_for_domain)
                domain = normalized_domain(website_url)
                outcome = company_snapshot.outcome if company_snapshot else "unknown"
                site_status = WebsiteStatus.ACTIVE if outcome == "reachable" else WebsiteStatus.INACTIVE if outcome == "inactive" else WebsiteStatus.UNKNOWN
                notes = [company_snapshot.error] if company_snapshot and company_snapshot.error else []

            candidate_id = stable_id("lead", f"{request.request_id}|{canonical_url(source_url)}")
            return Candidate(
                candidate_id=candidate_id,
                request_id=request.request_id,
                company_name=title,
                legal_name=title,
                domain=domain,
                website_url=website_url,
                description=(company_text or source_snapshot.text or raw.get("snippet", ""))[:2500],
                region_evidence=request.region if request.region.lower() in combined.lower() else None,
                employee_count=_parse_employee_count(combined),
                source_evidence=evidence,
                raw_search_snippet=raw_snippet,
                website_status=site_status,
                website_checked_at=(company_snapshot.fetched_at if company_snapshot else source_snapshot.fetched_at),
                contact_access=contact_access,
                verification_notes=notes,
            )

        research_failures: list[str] = []

        async def research_safely(raw: dict[str, Any]) -> Candidate:
            try:
                return await research(raw)
            except Exception as exc:
                research_failures.append(type(exc).__name__)
                # Keep a failed page visible as uncertain rather than losing all successful
                # candidates from the batch. Do not treat a directory URL as the company site.
                source_url = raw.get("url", "")
                title = _clean_company_name(raw.get("title", ""), normalized_domain(source_url))
                directory_source = is_business_directory(
                    source_url,
                    title=raw.get("title", ""),
                    snippet=raw.get("snippet", ""),
                )
                evidence = [evidence_from_text(
                    source_url,
                    raw.get("source_type", "web_search_result"),
                    f"{raw.get('title', title)}. {raw.get('snippet', '')}",
                )]
                website_url = "" if directory_source else site_root(source_url)
                domain = "" if directory_source else normalized_domain(website_url)
                return Candidate(
                    candidate_id=stable_id("lead", f"{request.request_id}|{canonical_url(source_url)}"),
                    request_id=request.request_id,
                    company_name=title,
                    legal_name=title,
                    domain=domain,
                    website_url=website_url,
                    description=(raw.get("snippet", "") or "")[:2500],
                    raw_search_snippet=raw.get("snippet", ""),
                    source_evidence=evidence,
                    website_status=WebsiteStatus.UNKNOWN,
                    verification_notes=[f"Page research failed and needs review ({type(exc).__name__}); no assumptions were made."],
                )

        semaphore_candidates = list(search_results)
        new_candidates = await asyncio.gather(*(research_safely(raw) for raw in semaphore_candidates))
        by_id = {candidate.candidate_id: candidate for candidate in _candidate_objects(state)}
        for candidate in new_candidates:
            by_id[candidate.candidate_id] = candidate
        # Research may reveal two search URLs for the same company only after their
        # pages have been fetched. Merge identities here as well as before fetching.
        combined_candidates = deduplicate_candidates(list(by_id.values()))
        return {
            "candidates": _candidate_dicts(combined_candidates),
            "events": [_event(
                "research_candidates",
                "partial_failure" if research_failures else "ok",
                f"Processed {len(new_candidates)} new candidate pages; {len(combined_candidates)} total before deduplication; {len(research_failures)} page research failure(s).",
                max_concurrency=settings.max_concurrency,
                failure_types=research_failures,
            )],
        }

    async def deep_qualify(state: AgentState) -> dict[str, Any]:
        request = LeadRequest.model_validate(state["request"])
        all_candidates = _candidate_objects(state)
        candidates = [c for c in all_candidates if c.decision != Decision.DUPLICATE and c.pre_qualified is not False]
        pending = [
            candidate for candidate in candidates
            if candidate.last_classified_evidence_hash != _candidate_evidence_hash(candidate)
        ]
        semaphore = asyncio.Semaphore(settings.max_concurrency)

        async def classify_batch(batch: list[Candidate]) -> dict[str, Any]:
            async with semaphore:
                return await classifier.classify_many(request, batch)

        batches = [pending[index:index + settings.llm_batch_size] for index in range(0, len(pending), settings.llm_batch_size)]
        batch_results = await asyncio.gather(*(classify_batch(batch) for batch in batches))
        classifications = {candidate_id: result for result in batch_results for candidate_id, result in result.items()}
        output = []
        for candidate in all_candidates:
            classification = classifications.get(candidate.candidate_id)
            if classification is None:
                output.append(candidate)
                continue
            output.append(candidate.model_copy(update={
                "decision": classification.decision,
                "confidence": classification.confidence,
                "rationale": classification.rationale,
                "matched_criteria": classification.matched_criteria,
                "missing_criteria": classification.missing_criteria,
                "decision_evidence_ids": classification.evidence_ids,
                "last_classified_evidence_hash": _candidate_evidence_hash(candidate),
            }))
        classified = [candidate for candidate in output if candidate.candidate_id in classifications]
        budget = current_budget()
        llm_active = classifier.llm is not None and (
            not settings.mock_mode or settings.llm_in_mock_mode or budget is not None
        )
        return {
            "candidates": _candidate_dicts(output),
            "events": [_event("deep_qualify", "ok", "Classified pre-screened candidates using evidence-backed criteria.",
                              qualified=sum(c.decision == Decision.QUALIFIED for c in classified),
                              rejected=sum(c.decision == Decision.REJECTED for c in classified),
                              uncertain=sum(c.decision == Decision.UNCERTAIN for c in classified),
                              reclassified=len(classified), batch_count=len(batches),
                              classifier="LLM with structured output and rule-based budget fallback" if llm_active and budget is not None else "LLM with structured output" if llm_active else "conservative deterministic fallback")],
        }

    async def extract_directory_links(state: AgentState) -> dict[str, Any]:
        # Directory pages are fetched once by the research adapter, which extracts only
        # explicit outbound company website links. Those candidates return through dedup.
        result = await research_candidates(state)
        return {**result, "events": result.get("events", []) + [_event("extract_directory_links", "ok", "Extracted company identities and explicit website links from directory pages.")]}

    async def targeted_research(state: AgentState) -> dict[str, Any]:
        request = LeadRequest.model_validate(state["request"])
        candidates = _candidate_objects(state)
        pending = [c for c in candidates if c.decision == Decision.UNCERTAIN and 0.50 <= c.confidence <= 0.70 and c.targeted_attempts < 1]
        pending.sort(key=lambda candidate: (
            candidate.website_status == WebsiteStatus.ACTIVE,
            candidate.confidence,
            len(candidate.matched_criteria),
        ), reverse=True)
        used = int(state.get("targeted_searches_used", 0))
        remaining = max(0, settings.max_targeted_searches_per_run - used)
        pending = pending[:remaining]
        usage = current_usage()
        if usage is not None:
            usage.increment("targeted_company_searches", len(pending))
        semaphore = asyncio.Semaphore(settings.max_concurrency)
        async def enrich(candidate: Candidate) -> Candidate:
            query = f'"{candidate.company_name}" {request.region} {" ".join(candidate.missing_criteria[:2])} company'
            try:
                results = await search_provider.search(query, 3)
                extra = [evidence_from_text(item.url, "targeted_search", f"{item.title}. {item.snippet}") for item in results[:3]]
                async with semaphore:
                    snapshot = await website_reader.fetch(candidate.website_url) if candidate.website_url else None
                if snapshot and snapshot.text:
                    extra.append(evidence_from_text(snapshot.final_url or candidate.website_url, "targeted_company_page", snapshot.text[:1500]))
                evidence_by_id = {item.evidence_id: item for item in candidate.source_evidence}
                evidence_by_id.update({item.evidence_id: item for item in extra})
                excerpts = " ".join(item.excerpt for item in extra)
                description = " ".join(f"{candidate.description} {excerpts}".split())[:2500]
                return candidate.model_copy(update={
                    "source_evidence": list(evidence_by_id.values()),
                    "description": description,
                    "targeted_attempts": candidate.targeted_attempts + 1,
                })
            except Exception:
                return candidate.model_copy(update={"targeted_attempts": candidate.targeted_attempts + 1})
        updated = {c.candidate_id: c for c in candidates}
        for c in await asyncio.gather(*(enrich(c) for c in pending)):
            updated[c.candidate_id] = c
        return {
            "candidates": _candidate_dicts(list(updated.values())),
            "targeted_searches_used": used + len(pending),
            "events": [_event(
                "targeted_research", "ok",
                f"Ran gap-filling research for {len(pending)} of {len(candidates)} eligible borderline candidates.",
                selected=len(pending), skipped=max(0, len([c for c in candidates if c.decision == Decision.UNCERTAIN and 0.50 <= c.confidence <= 0.70 and c.targeted_attempts < 1]) - len(pending)),
                run_limit=settings.max_targeted_searches_per_run,
            )],
        }

    async def deduplicate(state: AgentState) -> dict[str, Any]:
        # Before any page fetch, discard domains already in the CRM and URLs repeated
        # across discovery rounds. Directory links are intentionally retained as sources.
        pending = state.get("unprocessed_urls", state.get("search_results", []))
        candidates = _candidate_objects(state)
        known_domains: set[str] = set()
        known_names: set[str] = set()
        try:
            for record in await crm_adapter.list_companies():
                props = record.get("properties", {})
                domain = normalized_domain(str(props.get("domain", record.get("domain", ""))))
                if domain:
                    known_domains.add(domain)
                name = str(props.get("name", record.get("company_name", ""))).strip().lower()
                if name:
                    known_names.add(name)
        except Exception:
            # CRM lookup failure must not abort research; final upsert remains idempotent.
            pass
        seen = set(state.get("seen_source_urls", []))
        filtered: list[dict[str, Any]] = []
        duplicates = 0
        for raw in pending:
            url = str(raw.get("url", ""))
            domain = normalized_domain(url)
            title = str(raw.get("title", "")).strip().lower()
            if canonical_url(url) in seen or (domain and domain in known_domains) or (title and title in known_names):
                duplicates += 1
                continue
            seen.add(canonical_url(url))
            filtered.append(raw)
        original = _candidate_objects(state)
        deduped = deduplicate_candidates(original)
        duplicate_count = sum(c.decision == Decision.DUPLICATE for c in deduped)
        return {
            "unprocessed_urls": filtered,
            "seen_source_urls": sorted(seen),
            "candidates": _candidate_dicts(deduped),
            "events": [_event("deduplicate", "ok", f"Removed {duplicates} known or repeated search results before fetching; retained {len(filtered)} new URLs.", duplicate_count=duplicates + duplicate_count)],
        }

    async def fast_verify(state: AgentState) -> dict[str, Any]:
        pending = state.get("unprocessed_urls", [])
        # Avoid fetching every URL once here and again during evidence collection.
        # Research records page content; independent availability verification runs later.
        return {
            "unprocessed_urls": pending,
            "events": [_event(
                "fast_verify", "ok",
                f"Deferred availability checks for {len(pending)} URLs to evidence collection and final verification.",
                deferred=len(pending),
            )],
        }

    async def cheap_filter(state: AgentState) -> dict[str, Any]:
        # Fast, deterministic first pass over fetched titles/descriptions; the structured
        # classifier is reserved for candidates with a plausible industry signal.
        request = LeadRequest.model_validate(state["request"])
        industry = {t.lower() for t in re.findall(r"[a-z0-9]+", request.industry) if len(t) > 2}
        candidates = _candidate_objects(state)
        passed = []
        for candidate in candidates:
            text = " ".join([candidate.company_name, candidate.description, candidate.raw_search_snippet]).lower()
            hits = industry.intersection(re.findall(r"[a-z0-9]+", text))
            prequalified = bool(hits) or not industry
            passed.append(candidate.model_copy(update={"pre_qualified": prequalified}))
        return {"candidates": _candidate_dicts(passed), "events": [_event("cheap_filter", "ok", f"Fast triage retained {sum(bool(getattr(c, 'pre_qualified', False)) for c in passed)} candidates for full qualification.")]}

    async def verify_websites(state: AgentState) -> dict[str, Any]:
        candidates = _candidate_objects(state)
        semaphore = asyncio.Semaphore(settings.max_concurrency)
        eligible_ids = {
            candidate.candidate_id
            for candidate in candidates
            if candidate.decision == Decision.QUALIFIED and candidate.website_url
        }

        async def verify_one(candidate: Candidate) -> Candidate:
            if candidate.candidate_id not in eligible_ids:
                return candidate
            if not candidate.website_url:
                return candidate.model_copy(update={
                    "website_status": WebsiteStatus.UNKNOWN,
                    "website_checked_at": datetime.now(timezone.utc),
                    "verification_notes": candidate.verification_notes + ["No official company website URL was found; verification was not attempted."],
                })
            try:
                async with semaphore:
                    snapshot = await website_reader.verify(candidate.website_url)
            except Exception as exc:
                return candidate.model_copy(update={
                    "website_status": WebsiteStatus.UNKNOWN,
                    "website_checked_at": datetime.now(timezone.utc),
                    "verification_notes": candidate.verification_notes + [
                        f"Independent website verification failed ({type(exc).__name__}); status remains unknown."
                    ],
                })
            status = WebsiteStatus.ACTIVE if snapshot.outcome == "reachable" else WebsiteStatus.INACTIVE if snapshot.outcome == "inactive" else WebsiteStatus.UNKNOWN
            notes = list(candidate.verification_notes)
            if snapshot.error:
                notes.append(f"Verification: {snapshot.error}")
            if status == WebsiteStatus.INACTIVE:
                notes.append(f"Website did not pass the independent availability check (HTTP {snapshot.status_code}).")
            elif status == WebsiteStatus.UNKNOWN:
                notes.append("Website status is inconclusive; this is not treated as proof of inactivity.")
            if candidate.contact_access == ContactAccess.SKIPPED_REQUIRES_LOGIN or snapshot.contact_access == ContactAccess.SKIPPED_REQUIRES_LOGIN:
                contact_access = ContactAccess.SKIPPED_REQUIRES_LOGIN
            elif snapshot.contact_access != ContactAccess.UNKNOWN:
                contact_access = snapshot.contact_access
            else:
                contact_access = candidate.contact_access
            return candidate.model_copy(update={
                "website_status": status,
                "website_checked_at": snapshot.fetched_at,
                "contact_access": contact_access,
                "verification_notes": notes,
            })

        verified = await asyncio.gather(*(verify_one(candidate) for candidate in candidates))
        return {
            "candidates": _candidate_dicts(list(verified)),
            "events": [_event("verify_websites", "ok", "Independently re-checked qualified candidate websites.",
                              active=sum(c.website_status == WebsiteStatus.ACTIVE for c in verified),
                              inactive=sum(c.website_status == WebsiteStatus.INACTIVE for c in verified),
                              unknown=sum(c.website_status == WebsiteStatus.UNKNOWN for c in verified),
                              skipped=len(candidates) - len(eligible_ids))],
        }

    def route_after_discovery(state: AgentState) -> Literal["research_candidates", "prepare_proposal"]:
        if state.get("new_results_count", 0) == 0:
            return "prepare_proposal"
        return "research_candidates"

    def route_after_dedup(state: AgentState) -> Literal["extract_directory_links", "fast_verify", "discover_candidates", "prepare_proposal"]:
        request = LeadRequest.model_validate(state["request"])
        pending = state.get("unprocessed_urls", [])
        if pending and any(is_business_directory(r.get("url", ""), r.get("title", ""), r.get("snippet", "")) for r in pending):
            return "extract_directory_links"
        if pending:
            return "fast_verify"
        if int(state.get("search_round", 0)) < search_round_limit(request):
            return "discover_candidates"
        return "prepare_proposal"

    def route_after_cheap_filter(state: AgentState) -> Literal["deep_qualify", "discover_candidates", "prepare_proposal"]:
        request = LeadRequest.model_validate(state["request"])
        if any(c.pre_qualified for c in _candidate_objects(state)):
            return "deep_qualify"
        return "discover_candidates" if int(state.get("search_round", 0)) < search_round_limit(request) else "prepare_proposal"

    def route_after_deep_qualify(state: AgentState) -> Literal["targeted_research", "verify_websites"]:
        candidates = _candidate_objects(state)
        has_targetable = any(
            c.decision == Decision.UNCERTAIN and 0.50 <= c.confidence <= 0.70 and c.targeted_attempts < 1
            for c in candidates
        )
        has_budget = int(state.get("targeted_searches_used", 0)) < settings.max_targeted_searches_per_run
        if has_targetable and has_budget:
            return "targeted_research"
        return "verify_websites"

    def route_after_verification(state: AgentState) -> Literal["discover_candidates", "prepare_proposal"]:
        request = LeadRequest.model_validate(state["request"])
        candidates = _candidate_objects(state)
        active_qualified = sum(
            c.decision == Decision.QUALIFIED and c.website_status == WebsiteStatus.ACTIVE
            for c in candidates
        )
        if active_qualified >= request.target_leads or int(state.get("search_round", 0)) >= search_round_limit(request):
            return "prepare_proposal"
        return "discover_candidates"

    async def prepare_proposal(state: AgentState) -> dict[str, Any]:
        candidates = _candidate_objects(state)
        request = LeadRequest.model_validate(state["request"])
        eligible_all: list[Candidate] = []
        for candidate in candidates:
            if candidate.decision == Decision.QUALIFIED and candidate.website_status == WebsiteStatus.ACTIVE:
                eligible_all.append(candidate)
        eligible = eligible_all[: request.target_leads]
        selected_ids = {candidate.candidate_id for candidate in eligible}
        updated: list[Candidate] = []
        for candidate in candidates:
            if candidate.candidate_id in selected_ids:
                updated.append(candidate.model_copy(update={"import_status": ImportStatus.PENDING_APPROVAL}))
            elif candidate.decision == Decision.QUALIFIED and candidate.website_status == WebsiteStatus.INACTIVE:
                updated.append(candidate.model_copy(update={"import_status": ImportStatus.SKIPPED_INACTIVE}))
            else:
                updated.append(candidate)

        eligible_ids = [c.candidate_id for c in eligible]
        digest = _proposal_digest(eligible_ids, updated)
        proposal = {
            "proposal_version": 1,
            "request_id": state["request"]["request_id"],
            "proposal_hash": digest,
            "destination": "PostgreSQL CRM",
            "candidate_ids": eligible_ids,
            "changes": [
                {
                    "candidate_id": c.candidate_id,
                    "company_name": c.legal_name or c.company_name,
                    "domain": c.domain,
                    "website_url": c.website_url,
                    "qualification_reason": c.rationale,
                    "confidence": c.confidence,
                    "source_urls": list(dict.fromkeys(e.source_url for e in c.source_evidence)),
                    "decision_evidence_ids": c.decision_evidence_ids,
                }
                for c in eligible
            ],
        }
        approval_payload = {
            "kind": "crm_import_approval",
            "request_id": state["request"]["request_id"],
            "proposal_version": 1,
            "proposal_hash": digest,
            "destination": proposal["destination"],
            "candidate_count": len(eligible),
            "changes": proposal["changes"],
            "instructions": "Approve only after reviewing the exact destination and records. Select approved_candidate_ids from the displayed proposal.",
        }
        return {
            "candidates": _candidate_dicts(updated),
            "proposal": proposal,
            "proposal_hash": digest,
            "approval_payload": approval_payload,
            "events": [_event("prepare_proposal", "ok", f"Prepared {len(eligible)} active qualified leads for human approval.", proposal_hash=digest)],
        }

    def route_proposal(state: AgentState) -> Literal["approval_gate", "build_report"]:
        return "approval_gate" if state.get("proposal", {}).get("candidate_ids") else "build_report"

    async def approval_gate(state: AgentState) -> dict[str, Any]:
        # Keep all preparation outside the interrupt node. LangGraph restarts the node when resumed.
        raw = interrupt(state["approval_payload"])
        proposal_ids = set(state.get("proposal", {}).get("candidate_ids", []))
        try:
            approval = ApprovalDecision.model_validate(raw)
        except Exception as exc:
            normalized = {
                "approved": False,
                "status": "invalid_approval",
                "approved_candidate_ids": [],
                "reviewer_id": "unknown",
                "comment": f"Invalid approval payload: {exc}",
                "proposal_hash": "",
            }
            return {"approval": normalized, "events": [_event("approval_gate", "rejected", "Approval payload failed validation.")]}

        if approval.proposal_hash != state.get("proposal_hash"):
            normalized = approval.model_dump(mode="json") | {"approved": False, "status": "stale_proposal", "approved_candidate_ids": []}
            return {"approval": normalized, "events": [_event("approval_gate", "rejected", "Approval did not match the current proposal version.")]}
        chosen = set(approval.approved_candidate_ids)
        if not chosen.issubset(proposal_ids):
            normalized = approval.model_dump(mode="json") | {"approved": False, "status": "invalid_candidate_ids", "approved_candidate_ids": []}
            return {"approval": normalized, "events": [_event("approval_gate", "rejected", "Approval included candidate IDs outside the proposal.")]}
        status = "approved" if approval.approved and chosen else "rejected"
        normalized = approval.model_dump(mode="json") | {"approved": bool(approval.approved and chosen), "status": status, "approved_candidate_ids": sorted(chosen if approval.approved else set())}
        return {"approval": normalized, "events": [_event("approval_gate", status, f"Reviewer {approval.reviewer_id} {status} {len(normalized['approved_candidate_ids'])} lead(s).", reviewer_id=approval.reviewer_id)]}

    def route_approval(state: AgentState) -> Literal["execute_import", "build_report"]:
        approval = state.get("approval", {})
        return "execute_import" if approval.get("approved") and approval.get("approved_candidate_ids") else "build_report"

    async def execute_import(state: AgentState) -> dict[str, Any]:
        candidates = _candidate_objects(state)
        approval = state.get("approval", {})
        proposal_ids = state.get("proposal", {}).get("candidate_ids", [])
        current_digest = _proposal_digest(proposal_ids, candidates)
        if current_digest != state.get("proposal_hash") or approval.get("proposal_hash") != state.get("proposal_hash"):
            return {
                "error": "Proposal changed after approval; no CRM writes were executed.",
                "events": [_event("execute_import", "blocked", "Proposal hash validation failed; no writes executed.")],
            }
        approved_ids = set(approval.get("approved_candidate_ids", []))
        request = LeadRequest.model_validate(state["request"])
        result_rows: list[dict[str, Any]] = []
        output: list[Candidate] = []
        errors = 0
        for candidate in candidates:
            if candidate.candidate_id not in proposal_ids:
                output.append(candidate)
                continue
            if candidate.candidate_id not in approved_ids:
                output.append(candidate.model_copy(update={"import_status": ImportStatus.REJECTED_BY_REVIEWER}))
                continue
            try:
                result = await crm_adapter.upsert_company(
                    candidate,
                    idempotency_key=f"{request.request_id}:{candidate.candidate_id}",
                    request_region=request.region,
                )
                result_rows.append({
                    "candidate_id": candidate.candidate_id,
                    "status": result.get("status", "unknown"),
                    "record_id": result.get("id"),
                    "record_url": result.get("url"),
                    "message": "CRM upsert completed; awaiting independent read-back verification.",
                })
                status = ImportStatus.ALREADY_EXISTS if result.get("status") == "already_exists" else ImportStatus.IMPORTED
                output.append(candidate.model_copy(update={
                    "import_status": status,
                    "crm_record_id": str(result.get("id")) if result.get("id") else None,
                    "crm_record_url": result.get("url"),
                }))
            except Exception as exc:
                errors += 1
                result_rows.append({"candidate_id": candidate.candidate_id, "status": "failed", "record_id": None, "message": f"{type(exc).__name__}: {exc}"})
                output.append(candidate.model_copy(update={"import_status": ImportStatus.FAILED, "verification_notes": candidate.verification_notes + [f"CRM write failed: {type(exc).__name__}: {exc}"]}))
        return {
            "candidates": _candidate_dicts(output),
            "crm_results": result_rows,
            "events": [_event("execute_import", "ok" if not errors else "partial_failure", f"Attempted CRM writes for {len(result_rows)} approved lead(s).", failures=errors)],
        }

    async def verify_import(state: AgentState) -> dict[str, Any]:
        candidates = _candidate_objects(state)
        results_by_id = {r["candidate_id"]: r for r in state.get("crm_results", [])}
        updated: list[Candidate] = []
        verification_events = []
        for candidate in candidates:
            result = results_by_id.get(candidate.candidate_id)
            if not result or result.get("status") == "failed" or not result.get("record_id"):
                updated.append(candidate)
                continue
            try:
                observed = await crm_adapter.get_company(str(result["record_id"]))
                expected_name = (candidate.legal_name or candidate.company_name).strip().lower()
                properties = (observed or {}).get("properties", {})
                observed_name = str(properties.get("name", "")).strip().lower()
                observed_domain = normalized_domain(str(properties.get("domain", "")))
                match = bool(observed) and observed_name == expected_name and observed_domain == normalized_domain(candidate.domain)
                if match:
                    status = ImportStatus.ALREADY_EXISTS if result.get("status") == "already_exists" else ImportStatus.IMPORTED
                    updated.append(candidate.model_copy(update={"import_status": status, "crm_record_url": (observed or {}).get("url") or candidate.crm_record_url}))
                    result["verification"] = "verified"
                    result["observed_properties"] = properties
                else:
                    updated.append(candidate.model_copy(update={"import_status": ImportStatus.FAILED, "verification_notes": candidate.verification_notes + ["CRM read-back did not match the intended company name/domain."]}))
                    result["verification"] = "failed"
                    result["message"] = "Read-back mismatch or record not found."
                verification_events.append({"candidate_id": candidate.candidate_id, "verification": result["verification"]})
            except Exception as exc:
                updated.append(candidate.model_copy(update={"import_status": ImportStatus.FAILED, "verification_notes": candidate.verification_notes + [f"CRM verification failed: {type(exc).__name__}: {exc}"]}))
                result["verification"] = "failed"
                result["message"] = f"CRM verification failed: {type(exc).__name__}: {exc}"
                verification_events.append({"candidate_id": candidate.candidate_id, "verification": "failed"})
        return {
            "candidates": _candidate_dicts(updated),
            "crm_results": state.get("crm_results", []),
            "events": [_event("verify_import", "ok", "Read each created/existing CRM record back and compared intended properties.", records=verification_events)],
        }

    async def build_report(state: AgentState) -> dict[str, Any]:
        candidates = _candidate_objects(state)
        request = LeadRequest.model_validate(state["request"])
        run_id = state.get("run_id", "unknown")
        qualified = sum(c.decision == Decision.QUALIFIED for c in candidates)
        rejected = sum(c.decision == Decision.REJECTED for c in candidates)
        uncertain = sum(c.decision == Decision.UNCERTAIN for c in candidates)
        duplicates = sum(c.decision == Decision.DUPLICATE for c in candidates)
        inactive = sum(c.website_status == WebsiteStatus.INACTIVE for c in candidates)
        pending_review = sum(c.decision == Decision.UNCERTAIN or c.website_status == WebsiteStatus.UNKNOWN for c in candidates)
        imported = sum(c.import_status in {ImportStatus.IMPORTED, ImportStatus.ALREADY_EXISTS} for c in candidates)
        failures = sum(c.import_status == ImportStatus.FAILED for c in candidates)
        usage_counts = state.get("usage", {})
        llm_batch_calls = int(usage_counts.get("llm_batch_calls", 0))
        llm_http_attempts = int(usage_counts.get("llm_http_attempts", 0))
        usage_summary = RunUsageSummary(
            llm_batch_calls=llm_batch_calls,
            llm_http_attempts=llm_http_attempts,
            llm_retry_attempts=max(0, llm_http_attempts - llm_batch_calls),
            browserbase_search_requests=int(usage_counts.get("browserbase_search_requests", 0)),
            browserbase_fetch_requests=int(usage_counts.get("browserbase_fetch_requests", 0)),
            browserbase_session_requests=int(usage_counts.get("browserbase_session_requests", 0)),
            browserbase_total_requests=sum(int(usage_counts.get(name, 0)) for name in (
                "browserbase_search_requests", "browserbase_fetch_requests", "browserbase_session_requests"
            )),
            direct_fetch_requests=int(usage_counts.get("direct_fetch_requests", 0)),
            targeted_company_searches=int(usage_counts.get("targeted_company_searches", 0)),
        )
        notes: list[str] = []
        proposed_count = len(state.get("proposal", {}).get("candidate_ids", []))
        target_shortfall = max(0, request.target_leads - proposed_count)
        if target_shortfall:
            notes.append(
                f"Target was {request.target_leads}; {proposed_count} active qualified leads were found "
                f"within the search budget. Shortfall: {target_shortfall}."
            )
        if imported < request.target_leads and state.get("approval", {}).get("approved"):
            notes.append(
                f"{imported} CRM records were verified after review; the reviewer may have approved only a subset."
            )
        if uncertain:
            notes.append("Uncertain candidates were kept visible and were not auto-imported.")
        if inactive:
            notes.append("Inactive website results were retained for audit but excluded from the import proposal.")
        if settings.mock_mode and not request.test_mode:
            storage_note = (
                "CRM records were stored in memory and will be cleared when the service stops."
                if settings.database_url.startswith("memory://")
                else "CRM records were stored in PostgreSQL."
            )
            notes.append(f"This run used synthetic mock sources. {storage_note}")
        if request.test_mode:
            notes.append(
                f"Low-call test mode used {usage_summary.browserbase_total_requests} Browserbase requests "
                f"and {usage_summary.llm_batch_calls} LLM batch calls; qualification beyond the LLM cap uses rule-based evaluation."
            )
        report = AgentReport(
            request_id=request.request_id,
            run_id=run_id,
            requested_target=request.target_leads,
            target_reached=target_shortfall == 0,
            target_shortfall=target_shortfall,
            discovered_candidates=len(candidates),
            search_rounds=int(state.get("search_round", 0)),
            qualified=qualified,
            rejected=rejected,
            uncertain=uncertain,
            duplicates=duplicates,
            inactive=inactive,
            pending_review=pending_review,
            imported=imported,
            import_failures=failures,
            candidates=[c.model_dump(mode="json") for c in candidates],
            events=state.get("events", []),
            notes=notes,
            usage=usage_summary,
        )
        return {
            "candidates": _candidate_dicts(candidates),
            "report": report.model_dump(mode="json"),
            "events": [_event("build_report", "ok", "Built auditable final report.", usage=usage_summary.model_dump(mode="json"))],
        }

    def track_usage(node):
        async def tracked(state: AgentState) -> dict[str, Any]:
            tracker = current_usage()
            before = tracker.snapshot() if tracker is not None else None
            update = await node(state)
            if tracker is not None and before is not None:
                update["usage"] = usage_delta(before, tracker.snapshot())
            return update
        return tracked

    builder = StateGraph(AgentState)
    builder.add_node("validate_input", track_usage(validate_input))
    builder.add_node("discover_candidates", track_usage(discover_candidates))
    builder.add_node("deduplicate", track_usage(deduplicate))
    builder.add_node("extract_directory_links", track_usage(extract_directory_links))
    builder.add_node("fast_verify", track_usage(fast_verify))
    builder.add_node("research_candidates", track_usage(research_candidates))
    builder.add_node("cheap_filter", track_usage(cheap_filter))
    builder.add_node("deep_qualify", track_usage(deep_qualify))
    builder.add_node("targeted_research", track_usage(targeted_research))
    builder.add_node("verify_websites", track_usage(verify_websites))
    builder.add_node("prepare_proposal", track_usage(prepare_proposal))
    builder.add_node("approval_gate", track_usage(approval_gate))
    builder.add_node("execute_import", track_usage(execute_import))
    builder.add_node("verify_import", track_usage(verify_import))
    builder.add_node("build_report", track_usage(build_report))

    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "discover_candidates")
    builder.add_edge("discover_candidates", "deduplicate")
    builder.add_conditional_edges("deduplicate", route_after_dedup, {"extract_directory_links": "extract_directory_links", "fast_verify": "fast_verify", "discover_candidates": "discover_candidates", "prepare_proposal": "prepare_proposal"})
    builder.add_edge("extract_directory_links", "cheap_filter")
    builder.add_edge("fast_verify", "research_candidates")
    builder.add_edge("research_candidates", "cheap_filter")
    builder.add_conditional_edges("cheap_filter", route_after_cheap_filter, {"deep_qualify": "deep_qualify", "discover_candidates": "discover_candidates", "prepare_proposal": "prepare_proposal"})
    builder.add_conditional_edges("deep_qualify", route_after_deep_qualify, {"targeted_research": "targeted_research", "verify_websites": "verify_websites"})
    builder.add_edge("targeted_research", "deep_qualify")
    builder.add_conditional_edges("verify_websites", route_after_verification, {"discover_candidates": "discover_candidates", "prepare_proposal": "prepare_proposal"})
    builder.add_conditional_edges("prepare_proposal", route_proposal, {"approval_gate": "approval_gate", "build_report": "build_report"})
    builder.add_conditional_edges("approval_gate", route_approval, {"execute_import": "execute_import", "build_report": "build_report"})
    builder.add_edge("execute_import", "verify_import")
    builder.add_edge("verify_import", "build_report")
    builder.add_edge("build_report", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())
