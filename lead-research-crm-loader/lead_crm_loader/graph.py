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
    normalized_domain,
    stable_id,
    site_root,
)


class AgentState(TypedDict, total=False):
    request: dict[str, Any]
    run_id: str
    search_results: list[dict[str, Any]]
    seen_urls: list[str]
    search_round: int
    new_results_count: int
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
        remaining_budget = max(0, settings.max_candidates - len(existing_urls))
        size_text = ""
        if request.company_size.min_employees or request.company_size.max_employees:
            size_text = f" companies with {request.company_size.min_employees or 1}-{request.company_size.max_employees or 'many'} employees"
        base = f"{request.industry} companies in {request.region}{size_text}"
        if round_number == 1:
            queries = [
                f"{base} official website",
                f"{base} company directory",
                f"{base} B2B companies company profiles",
                f"{request.industry} startups and established companies {request.region} website",
                f"{request.region} {request.industry} vendors products businesses",
            ]
        elif round_number == 2:
            criteria_phrase = " ".join(request.qualification_criteria[:3])
            queries = [
                f"{request.region} {request.industry} {criteria_phrase} companies official",
                f"list of {request.industry} companies in {request.region} with websites",
                f"{request.region} technology park {request.industry} company directory",
                f"{request.industry} companies serving businesses {request.region} company profile",
                f"{base} association member directory product companies",
            ]
        else:
            criteria_phrase = " ".join(request.qualification_criteria[:3])
            queries = [
                f"{request.industry} firms {request.region} verified website {criteria_phrase}",
                f"{request.region} {request.industry} vendors annual report portfolio companies",
                f"{base} examples clients solutions official contact about",
                f"{request.region} {request.industry} company registry business listing",
                f"{criteria_phrase} {request.industry} {request.region} business directory",
            ]
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
        for batch in batches:
            for item in batch:
                try:
                    normalized = canonical_url(item.url)
                    domain = normalized_domain(item.url)
                    if not domain or normalized in seen:
                        continue
                    seen.add(normalized)
                    found.append(item.model_copy(update={"url": item.url.strip()}))
                except Exception:
                    continue
                if len(found) >= remaining_budget:
                    break
            if len(found) >= remaining_budget:
                break
        return {
            "search_results": [item.model_dump(mode="json") for item in found],
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
                failed_query_errors=failed_queries,
            )],
        }

    async def research_candidates(state: AgentState) -> dict[str, Any]:
        request = LeadRequest.model_validate(state["request"])
        search_results = state.get("search_results", [])
        semaphore = asyncio.Semaphore(settings.max_concurrency)

        async def research(raw: dict[str, Any]) -> Candidate:
            source_url = raw["url"]
            async with semaphore:
                source_snapshot = await website_reader.fetch(source_url)
            source_title = source_snapshot.h1 or source_snapshot.title
            title = _clean_company_name(
                source_title if source_snapshot.outcome == "reachable" else "",
                raw.get("title", normalized_domain(source_url)),
            )
            directory_source = is_business_directory(
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
                        official_title = company_snapshot.h1 or company_snapshot.title
                        if official_title:
                            title = _clean_company_name(official_title, title)
                else:
                    company_snapshot = None

            evidence = [evidence_from_text(
                source_url,
                raw.get("source_type", "web_search_result"),
                f"{raw.get('title', title)}. {raw.get('snippet', '')}",
            )]
            if source_snapshot.text:
                evidence.append(evidence_from_text(source_snapshot.final_url or source_url, "source_page", source_snapshot.text[:2500]))
            company_text = ""
            contact_access = ContactAccess.UNKNOWN
            if company_snapshot is not None:
                company_text = " ".join([company_snapshot.meta_description, company_snapshot.text]).strip()
                if company_snapshot.text:
                    evidence.append(evidence_from_text(company_snapshot.final_url or official_url or source_url, "company_website", company_text[:2500]))
                contact_access = company_snapshot.contact_access
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

            combined = " ".join([raw.get("snippet", ""), source_snapshot.text, company_text])
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
                raw_search_snippet=raw.get("snippet", ""),
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
        combined_candidates = list(by_id.values())
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

    async def qualify_candidates(state: AgentState) -> dict[str, Any]:
        request = LeadRequest.model_validate(state["request"])
        candidates = _candidate_objects(state)
        semaphore = asyncio.Semaphore(settings.max_concurrency)

        async def classify_one(candidate: Candidate) -> Candidate:
            async with semaphore:
                classification = await classifier.classify(request, candidate)
            return candidate.model_copy(update={
                "decision": classification.decision,
                "confidence": classification.confidence,
                "rationale": classification.rationale,
                "matched_criteria": classification.matched_criteria,
                "missing_criteria": classification.missing_criteria,
                "decision_evidence_ids": classification.evidence_ids,
            })

        classified = await asyncio.gather(*(classify_one(candidate) for candidate in candidates))
        return {
            "candidates": _candidate_dicts(list(classified)),
            "events": [_event("qualify_candidates", "ok", "Classified candidates using evidence-backed criteria.",
                              qualified=sum(c.decision == Decision.QUALIFIED for c in classified),
                              rejected=sum(c.decision == Decision.REJECTED for c in classified),
                              uncertain=sum(c.decision == Decision.UNCERTAIN for c in classified),
                              classifier="LLM with structured output" if classifier.llm else "conservative deterministic fallback")],
        }

    async def deduplicate(state: AgentState) -> dict[str, Any]:
        original = _candidate_objects(state)
        deduped = deduplicate_candidates(original)
        duplicate_count = sum(c.decision == Decision.DUPLICATE for c in deduped)
        return {
            "candidates": _candidate_dicts(deduped),
            "events": [_event("deduplicate", "ok", f"Collapsed duplicate company identities; retained {len(deduped) - duplicate_count} canonical candidates.", duplicate_count=duplicate_count)],
        }

    async def verify_websites(state: AgentState) -> dict[str, Any]:
        candidates = _candidate_objects(state)
        semaphore = asyncio.Semaphore(settings.max_concurrency)

        async def verify_one(candidate: Candidate) -> Candidate:
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
            "events": [_event("verify_websites", "ok", "Independently re-checked candidate websites.",
                              active=sum(c.website_status == WebsiteStatus.ACTIVE for c in verified),
                              inactive=sum(c.website_status == WebsiteStatus.INACTIVE for c in verified),
                              unknown=sum(c.website_status == WebsiteStatus.UNKNOWN for c in verified))],
        }

    def route_after_discovery(state: AgentState) -> Literal["research_candidates", "prepare_proposal"]:
        if state.get("new_results_count", 0) == 0:
            return "prepare_proposal"
        return "research_candidates"

    def route_after_verification(state: AgentState) -> Literal["discover_candidates", "prepare_proposal"]:
        request = LeadRequest.model_validate(state["request"])
        candidates = _candidate_objects(state)
        active_qualified = sum(
            c.decision == Decision.QUALIFIED and c.website_status == WebsiteStatus.ACTIVE
            for c in candidates
        )
        if active_qualified >= request.target_leads or int(state.get("search_round", 0)) >= settings.max_search_rounds:
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
        if settings.mock_mode:
            storage_note = (
                "CRM records were stored in memory and will be cleared when the service stops."
                if settings.database_url.startswith("memory://")
                else "CRM records were stored in PostgreSQL."
            )
            notes.append(f"This run used synthetic mock sources. {storage_note}")
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
        )
        return {"candidates": _candidate_dicts(candidates), "report": report.model_dump(mode="json"), "events": [_event("build_report", "ok", "Built auditable final report.")]}

    builder = StateGraph(AgentState)
    builder.add_node("validate_input", validate_input)
    builder.add_node("discover_candidates", discover_candidates)
    builder.add_node("research_candidates", research_candidates)
    builder.add_node("qualify_candidates", qualify_candidates)
    builder.add_node("deduplicate", deduplicate)
    builder.add_node("verify_websites", verify_websites)
    builder.add_node("prepare_proposal", prepare_proposal)
    builder.add_node("approval_gate", approval_gate)
    builder.add_node("execute_import", execute_import)
    builder.add_node("verify_import", verify_import)
    builder.add_node("build_report", build_report)

    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "discover_candidates")
    builder.add_conditional_edges("discover_candidates", route_after_discovery, {"research_candidates": "research_candidates", "prepare_proposal": "prepare_proposal"})
    builder.add_edge("research_candidates", "qualify_candidates")
    builder.add_edge("qualify_candidates", "deduplicate")
    builder.add_edge("deduplicate", "verify_websites")
    builder.add_conditional_edges("verify_websites", route_after_verification, {"discover_candidates": "discover_candidates", "prepare_proposal": "prepare_proposal"})
    builder.add_conditional_edges("prepare_proposal", route_proposal, {"approval_gate": "approval_gate", "build_report": "build_report"})
    builder.add_conditional_edges("approval_gate", route_approval, {"execute_import": "execute_import", "build_report": "build_report"})
    builder.add_edge("execute_import", "verify_import")
    builder.add_edge("verify_import", "build_report")
    builder.add_edge("build_report", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())
