from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from .config import Settings
from .call_budget import LLMBudgetExhausted, current_budget
from .models import BatchClassification, Candidate, CandidateClassification, Classification, Decision, LeadRequest, WebsiteStatus
from .usage import current_usage

STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "are", "is", "in", "of", "to", "a", "an",
    "company", "companies", "business", "businesses", "based", "located", "provides", "provide", "builds", "building",
    "using", "use", "working", "website", "evidence", "product", "products", "service", "services", "must", "should",
}


def tokens(value: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[a-zA-Z0-9]+", value) if t.lower() not in STOP_WORDS and len(t) >= 2}


class RuleBasedClassifier:
    """Conservative no-key fallback for local demos and tests; not a replacement for an LLM in live runs."""

    def __init__(self, min_confidence: float = 0.70) -> None:
        self.min_confidence = min_confidence

    async def classify(self, request: LeadRequest, candidate: Candidate) -> Classification:
        evidence_text = " ".join([candidate.raw_search_snippet, candidate.description, *[e.excerpt for e in candidate.source_evidence]]).lower()
        evidence_ids = [e.evidence_id for e in candidate.source_evidence]
        if not evidence_text.strip() or not evidence_ids:
            return Classification(
                decision=Decision.UNCERTAIN,
                confidence=0.15,
                rationale="Insufficient public evidence to evaluate this company.",
                missing_criteria=request.qualification_criteria,
                evidence_ids=evidence_ids,
            )

        evidence_tokens = set(re.findall(r"[a-zA-Z0-9]+", evidence_text.lower()))
        industry_tokens = tokens(request.industry)
        criteria_tokens = set().union(*(tokens(c) for c in request.qualification_criteria))
        matched_industry = sorted(t for t in industry_tokens if t in evidence_tokens)
        region_matches = request.region.lower() in evidence_text
        matched_criteria_tokens = sorted(t for t in criteria_tokens if t in evidence_tokens)
        criterion_coverage = len(matched_criteria_tokens) / max(1, len(criteria_tokens))

        missing: list[str] = []
        if not matched_industry:
            return Classification(
                decision=Decision.REJECTED,
                confidence=0.88,
                rationale=f"Public evidence does not substantiate the requested industry ({request.industry}).",
                missing_criteria=[f"Industry match: {request.industry}"],
                evidence_ids=evidence_ids,
            )
        if not region_matches:
            missing.append(f"Location evidence for {request.region}")
        if criterion_coverage < 0.35:
            missing.extend(request.qualification_criteria)
        if request.company_size.min_employees is not None or request.company_size.max_employees is not None:
            if candidate.employee_count is None:
                missing.append("Verified employee count within the requested range")
            elif request.company_size.min_employees and candidate.employee_count < request.company_size.min_employees:
                missing.append("Employee count meets minimum")
            elif request.company_size.max_employees and candidate.employee_count > request.company_size.max_employees:
                missing.append("Employee count meets maximum")

        if missing:
            # Lack of evidence is not the same as evidence of failure.
            return Classification(
                decision=Decision.UNCERTAIN,
                confidence=max(0.25, min(0.68, 0.40 + criterion_coverage * 0.25)),
                rationale="Some criteria are supported, but required evidence is missing or incomplete.",
                matched_criteria=[f"Industry evidence: {', '.join(matched_industry)}"] + (["Qualification keywords found"] if criterion_coverage >= 0.35 else []),
                missing_criteria=missing,
                evidence_ids=evidence_ids,
            )

        confidence = min(0.92, 0.70 + criterion_coverage * 0.20 + (0.05 if region_matches else 0.0))
        return Classification(
            decision=Decision.QUALIFIED,
            confidence=confidence,
            rationale="Public sources support the requested industry, region, and qualification criteria.",
            matched_criteria=[f"Industry: {request.industry}", f"Region: {request.region}", "Qualification criteria supported by source text"],
            missing_criteria=[],
            evidence_ids=evidence_ids,
        )


class LLMClassifier:
    def __init__(self, settings: Settings) -> None:
        from langchain_openai import ChatOpenAI

        async def count_http_attempt(_request: httpx.Request) -> None:
            usage = current_usage()
            if usage is not None:
                usage.increment("llm_http_attempts")

        # The injected client observes each HTTP attempt, including SDK-managed retries.
        self.http_client = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            event_hooks={"request": [count_http_attempt]},
        )
        kwargs: dict[str, object] = {
            "model": settings.llm_model,
            "api_key": settings.llm_api_key,
            "temperature": 0,
            "timeout": settings.request_timeout_seconds,
            "max_retries": 2,
            "http_async_client": self.http_client,
        }
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        default_headers: dict[str, str] = {}
        if settings.openrouter_api_key and settings.llm_api_key == settings.openrouter_api_key and settings.llm_base_url and "openrouter.ai" in settings.llm_base_url:
            if settings.openrouter_site_url:
                default_headers["HTTP-Referer"] = settings.openrouter_site_url
            if settings.openrouter_app_name:
                default_headers["X-Title"] = settings.openrouter_app_name
        if default_headers:
            kwargs["default_headers"] = default_headers
        self.model = ChatOpenAI(**kwargs).with_structured_output(BatchClassification)
        # Keep test-mode provider attempts strict by disabling the client's retries.
        self.limited_model = ChatOpenAI(**{**kwargs, "max_retries": 0}).with_structured_output(BatchClassification)

    @staticmethod
    def _candidate_payload(candidate: Candidate) -> dict[str, Any]:
        return {
            "candidate_id": candidate.candidate_id,
            "company_name": candidate.company_name,
            "legal_name": candidate.legal_name,
            "domain": candidate.domain,
            "website_status": candidate.website_status,
            "employee_count": candidate.employee_count,
            "description": candidate.description[:1000],
            "raw_search_snippet": candidate.raw_search_snippet[:500],
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "source_url": item.source_url,
                    "source_type": item.source_type,
                    "excerpt": item.excerpt[:600],
                }
                for item in candidate.source_evidence[:3]
            ],
        }

    async def classify_many(
        self, request: LeadRequest, candidates: list[Candidate]
    ) -> list[CandidateClassification]:
        prompt = [
            (
                "system",
                """You are a conservative B2B company-qualification engine. Evaluate every supplied company independently. """
                "Never mix facts or evidence between companies. Use only the evidence attached to that candidate. """
                "Never infer missing location, employee count, products, or legal identity. Do not use an inaccessible contact page as evidence. """
                "Choose qualified only when evidence supports the industry, region, every material qualification criterion, and any specified size range. """
                "Choose rejected only when evidence contradicts a material criterion; choose uncertain when evidence is missing or ambiguous. """
                "For qualified results, cite evidence that directly supports the region and every material criterion, including at least one company_website record. "
                "When a criterion requires products, platforms, SaaS, or tools, the official company_website evidence itself must show the relevant AI/software offering; do not rely on a listicle or a search snippet. "
                "Return exactly one classification for each candidate_id. Include only exact evidence_ids supplied for that same candidate."
            ),
            (
                "human",
                "REQUEST: " + request.model_dump_json() + "\nCANDIDATES: " + json.dumps(
                    [self._candidate_payload(candidate) for candidate in candidates],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        ]
        budget = current_budget()
        model = self.model
        if budget is not None:
            if not budget.reserve_llm():
                raise LLMBudgetExhausted("Test-mode LLM call budget exhausted")
            model = self.limited_model
        usage = current_usage()
        if usage is not None:
            usage.increment("llm_batch_calls")
        result = await model.ainvoke(prompt)
        batch = result if isinstance(result, BatchClassification) else BatchClassification.model_validate(result)
        return batch.classifications

    async def classify(self, request: LeadRequest, candidate: Candidate) -> Classification:
        results = await self.classify_many(request, [candidate])
        return results[0]


class QualificationService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.fallback = RuleBasedClassifier(settings.min_confidence)
        self.llm = None
        if settings.llm_api_key:
            try:
                self.llm = LLMClassifier(settings)
            except Exception:
                # An unavailable optional provider must not prevent the mock demo from starting.
                self.llm = None

    async def classify_many(self, request: LeadRequest, candidates: list[Candidate]) -> dict[str, Classification]:
        budget = current_budget()
        if self.llm is None or (self.settings.mock_mode and not self.settings.llm_in_mock_mode and budget is None):
            fallback_results = await asyncio.gather(*(self.fallback.classify(request, candidate) for candidate in candidates))
            return {candidate.candidate_id: result for candidate, result in zip(candidates, fallback_results)}
        try:
            raw_results = await self.llm.classify_many(request, candidates)
        except LLMBudgetExhausted:
            fallback_results = await asyncio.gather(*(self.fallback.classify(request, candidate) for candidate in candidates))
            return {candidate.candidate_id: result for candidate, result in zip(candidates, fallback_results)}
        except Exception:
            # Mark low-confidence rather than silently promoting a failed model call.
            return {
                candidate.candidate_id: self._uncertain(request, candidate, "LLM batch classification failed; candidate requires manual review.")
                for candidate in candidates
            }

        by_id: dict[str, CandidateClassification | None] = {}
        duplicate_ids: set[str] = set()
        valid_candidate_ids = {candidate.candidate_id for candidate in candidates}
        for result in raw_results:
            if result.candidate_id not in valid_candidate_ids:
                continue
            if result.candidate_id in by_id:
                duplicate_ids.add(result.candidate_id)
            by_id[result.candidate_id] = result

        output: dict[str, Classification] = {}
        for candidate in candidates:
            result = by_id.get(candidate.candidate_id)
            if result is None or candidate.candidate_id in duplicate_ids:
                output[candidate.candidate_id] = self._uncertain(
                    request, candidate, "LLM batch omitted or duplicated this candidate; manual review is required."
                )
            else:
                output[candidate.candidate_id] = self._validate_result(request, candidate, result)
        return output

    async def classify(self, request: LeadRequest, candidate: Candidate) -> Classification:
        return (await self.classify_many(request, [candidate]))[candidate.candidate_id]

    @staticmethod
    def _uncertain(request: LeadRequest, candidate: Candidate, rationale: str) -> Classification:
        return Classification(
            decision=Decision.UNCERTAIN,
            confidence=0.20,
            rationale=rationale,
            missing_criteria=request.qualification_criteria,
            evidence_ids=[e.evidence_id for e in candidate.source_evidence],
        )

    def _validate_result(
        self, request: LeadRequest, candidate: Candidate, result: Classification
    ) -> Classification:
        valid_ids = {e.evidence_id for e in candidate.source_evidence}
        cited_ids = [value for value in result.evidence_ids if value in valid_ids]
        cited_text = " ".join(
            evidence.excerpt
            for evidence in candidate.source_evidence
            if evidence.evidence_id in cited_ids
        ).lower()
        official_cited_text = " ".join(
            evidence.excerpt
            for evidence in candidate.source_evidence
            if evidence.evidence_id in cited_ids and evidence.source_type == "company_website"
        ).lower()
        evidence_tokens = set(re.findall(r"[a-zA-Z0-9]+", cited_text))
        official_tokens = set(re.findall(r"[a-zA-Z0-9]+", official_cited_text))
        # Enforce numeric constraints in ordinary code; don't let a model infer employee count.
        size = request.company_size
        size_requested = size.min_employees is not None or size.max_employees is not None
        if result.decision == Decision.QUALIFIED and size_requested and candidate.employee_count is None:
            return result.model_copy(update={
                "decision": Decision.UNCERTAIN,
                "rationale": "Employee count is not supported by public evidence; size eligibility cannot be verified.",
                "missing_criteria": result.missing_criteria + ["Verified employee count in requested range"],
                "evidence_ids": cited_ids,
            })
        if result.decision == Decision.QUALIFIED and size.min_employees is not None and candidate.employee_count is not None and candidate.employee_count < size.min_employees:
            return result.model_copy(update={"decision": Decision.REJECTED, "rationale": "Verified employee count is below the requested minimum.", "evidence_ids": cited_ids})
        if result.decision == Decision.QUALIFIED and size.max_employees is not None and candidate.employee_count is not None and candidate.employee_count > size.max_employees:
            return result.model_copy(update={"decision": Decision.REJECTED, "rationale": "Verified employee count exceeds the requested maximum.", "evidence_ids": cited_ids})
        # Post-validate the model's structured response. The model is not allowed to invent evidence references.
        if result.decision == Decision.QUALIFIED and (not cited_ids or result.confidence < self.settings.min_confidence):
            return result.model_copy(update={
                "decision": Decision.UNCERTAIN,
                "rationale": "Model qualification lacked sufficient confidence or valid evidence citations.",
                "evidence_ids": cited_ids,
            })
        if result.decision == Decision.QUALIFIED:
            unsupported: list[str] = []
            industry_tokens = tokens(request.industry)
            if industry_tokens and not industry_tokens.intersection(evidence_tokens):
                unsupported.append(f"Industry evidence for {request.industry}")
            if request.region and request.region.lower() not in cited_text:
                unsupported.append(f"Location evidence for {request.region}")
            if request.region and official_cited_text and request.region.lower() not in official_cited_text:
                unsupported.append(f"Location evidence for {request.region} on the official website")
            if not candidate.domain or not candidate.website_url or candidate.website_status != WebsiteStatus.ACTIVE:
                unsupported.append("An active official company website")
            if not any(e.source_type == "company_website" and e.evidence_id in cited_ids for e in candidate.source_evidence):
                unsupported.append("Evidence from the official company website")

            for criterion in request.qualification_criteria:
                lowered = criterion.lower()
                if any(signal in lowered for signal in ("website", "official", "active")):
                    continue
                if any(signal in lowered for signal in ("based", "located", "headquartered")) or request.region.lower() in lowered:
                    continue
                criterion_tokens = tokens(criterion)
                coverage = len(criterion_tokens.intersection(evidence_tokens)) / max(1, len(criterion_tokens))
                if coverage < 0.35:
                    unsupported.append(f"Evidence for criterion: {criterion}")
                if re.search(r"\b(products?|platforms?|saas|tools?)\b", lowered) and not re.search(
                    r"\b(products?|platforms?|saas|tools?)\b", cited_text
                ):
                    unsupported.append(f"Evidence of a product or platform for criterion: {criterion}")
                if re.search(r"\b(products?|platforms?|saas|tools?)\b", lowered):
                    official_product = re.search(r"\b(products?|platforms?|saas|tools?)\b", official_cited_text)
                    official_industry = tokens(request.industry).intersection(official_tokens)
                    if not official_product or not official_industry:
                        unsupported.append(
                            f"Official-site evidence of an AI/software product or platform for criterion: {criterion}"
                        )

            if unsupported:
                return result.model_copy(update={
                    "decision": Decision.UNCERTAIN,
                    "confidence": min(result.confidence, 0.68),
                    "rationale": "The cited public evidence does not substantiate every required criterion: " + "; ".join(dict.fromkeys(unsupported)) + ".",
                    "missing_criteria": list(dict.fromkeys(result.missing_criteria + unsupported)),
                    "evidence_ids": cited_ids,
                })
        return result.model_copy(update={"evidence_ids": cited_ids})
