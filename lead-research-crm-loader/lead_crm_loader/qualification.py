from __future__ import annotations

import re
from typing import Iterable

from .config import Settings
from .models import Candidate, Classification, Decision, LeadRequest

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

        kwargs: dict[str, object] = {
            "model": settings.llm_model,
            "api_key": settings.llm_api_key,
            "temperature": 0,
            "timeout": settings.request_timeout_seconds,
            "max_retries": 2,
        }
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        default_headers: dict[str, str] = {}
        if settings.openrouter_api_key:
            if settings.openrouter_site_url:
                default_headers["HTTP-Referer"] = settings.openrouter_site_url
            if settings.openrouter_app_name:
                default_headers["X-Title"] = settings.openrouter_app_name
        if default_headers:
            kwargs["default_headers"] = default_headers
        self.model = ChatOpenAI(**kwargs).with_structured_output(Classification)

    async def classify(self, request: LeadRequest, candidate: Candidate) -> Classification:
        evidence = [
            {
                "evidence_id": item.evidence_id,
                "source_url": item.source_url,
                "source_type": item.source_type,
                "excerpt": item.excerpt,
            }
            for item in candidate.source_evidence
        ]
        prompt = [
            (
                "system",
                """You are a conservative B2B company-qualification engine. Use only the supplied public evidence. """
                "Never infer missing location, employee count, products, or legal identity. Do not use an inaccessible contact page as evidence. """
                "Choose qualified only when evidence supports the industry, region, every material qualification criterion, and any specified size range. """
                "Choose rejected only when evidence contradicts a material criterion; choose uncertain when evidence is missing/ambiguous. """
                "Return short, auditable reasons; include exact evidence_ids from the supplied list that support the decision."
            ),
            (
                "human",
                f"REQUEST: {request.model_dump_json()}\nCANDIDATE: {candidate.model_dump_json()}\nEVIDENCE: {evidence}",
            ),
        ]
        result = await self.model.ainvoke(prompt)
        return result if isinstance(result, Classification) else Classification.model_validate(result)


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

    async def classify(self, request: LeadRequest, candidate: Candidate) -> Classification:
        if self.llm is None:
            return await self.fallback.classify(request, candidate)
        try:
            result = await self.llm.classify(request, candidate)
        except Exception:
            # Mark low-confidence rather than silently promoting a failed model call.
            return Classification(
                decision=Decision.UNCERTAIN,
                confidence=0.20,
                rationale="LLM classification failed; candidate requires manual review.",
                missing_criteria=request.qualification_criteria,
                evidence_ids=[e.evidence_id for e in candidate.source_evidence],
            )

        valid_ids = {e.evidence_id for e in candidate.source_evidence}
        cited_ids = [value for value in result.evidence_ids if value in valid_ids]
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
        return result.model_copy(update={"evidence_ids": cited_ids})
