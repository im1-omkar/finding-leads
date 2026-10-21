from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class CompanySize(StrictModel):
    min_employees: int | None = Field(default=None, ge=1)
    max_employees: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> "CompanySize":
        if (
            self.min_employees is not None
            and self.max_employees is not None
            and self.min_employees > self.max_employees
        ):
            raise ValueError("min_employees cannot exceed max_employees")
        return self


class LeadRequest(StrictModel):
    request_id: str = Field(min_length=3, max_length=100)
    industry: str = Field(min_length=2, max_length=160)
    region: str = Field(min_length=2, max_length=160)
    company_size: CompanySize = Field(default_factory=CompanySize)
    qualification_criteria: list[str] = Field(min_length=1, max_length=20)
    target_leads: int = Field(default=50, ge=1, le=200)
    test_mode: bool = False

    @field_validator("request_id", "industry", "region")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Field cannot be blank")
        return value

    @field_validator("qualification_criteria")
    @classmethod
    def clean_criteria(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values if v and v.strip()]
        if not cleaned:
            raise ValueError("At least one non-empty qualification criterion is required")
        return cleaned


class Decision(StrEnum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"
    DUPLICATE = "duplicate"


class WebsiteStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class ContactAccess(StrEnum):
    NOT_NEEDED = "not_needed"
    PUBLIC = "public"
    SKIPPED_REQUIRES_LOGIN = "skipped_requires_login"
    UNKNOWN = "unknown"


class ImportStatus(StrEnum):
    NOT_PROPOSED = "not_proposed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    IMPORTED = "imported"
    ALREADY_EXISTS = "already_exists"
    FAILED = "failed"
    REJECTED_BY_REVIEWER = "rejected_by_reviewer"
    SKIPPED_INACTIVE = "skipped_inactive"


class Evidence(StrictModel):
    evidence_id: str
    source_url: str
    source_type: str
    retrieved_at: datetime = Field(default_factory=utc_now)
    excerpt: str = Field(max_length=3000)
    content_hash: str | None = None


class SearchResult(StrictModel):
    title: str
    url: str
    snippet: str = ""
    source_type: str = "business_directory"


class Classification(StrictModel):
    decision: Decision
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    matched_criteria: list[str] = Field(default_factory=list)
    missing_criteria: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class WebsiteSnapshot(StrictModel):
    requested_url: str
    final_url: str | None = None
    status_code: int | None = None
    title: str = ""
    h1: str = ""
    meta_description: str = ""
    text: str = ""
    links: list[dict[str, str]] = Field(default_factory=list)
    fetched_at: datetime = Field(default_factory=utc_now)
    outcome: str = "unknown"  # reachable | inactive | unknown
    error: str | None = None
    contact_access: ContactAccess = ContactAccess.UNKNOWN


class Candidate(StrictModel):
    candidate_id: str
    request_id: str
    company_name: str
    legal_name: str | None = None
    domain: str
    website_url: str
    description: str = ""
    region_evidence: str | None = None
    employee_count: int | None = Field(default=None, ge=0)
    source_evidence: list[Evidence] = Field(default_factory=list)
    raw_search_snippet: str = ""
    decision: Decision = Decision.UNCERTAIN
    pre_qualified: bool | None = None
    targeted_attempts: int = 0
    confidence: float = 0.0
    rationale: str = "Not yet evaluated"
    matched_criteria: list[str] = Field(default_factory=list)
    missing_criteria: list[str] = Field(default_factory=list)
    decision_evidence_ids: list[str] = Field(default_factory=list)
    duplicate_of: str | None = None
    alias_domains: list[str] = Field(default_factory=list)
    website_status: WebsiteStatus = WebsiteStatus.UNKNOWN
    website_checked_at: datetime | None = None
    contact_access: ContactAccess = ContactAccess.UNKNOWN
    import_status: ImportStatus = ImportStatus.NOT_PROPOSED
    crm_record_id: str | None = None
    crm_record_url: str | None = None
    verification_notes: list[str] = Field(default_factory=list)

    @field_validator("company_name")
    @classmethod
    def company_name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return "Unknown company"
        return value[:240]


class ApprovalDecision(StrictModel):
    approved: bool
    proposal_hash: str
    approved_candidate_ids: list[str] = Field(default_factory=list)
    reviewer_id: str = Field(min_length=1, max_length=160)
    comment: str | None = Field(default=None, max_length=2000)


class CRMWriteResult(StrictModel):
    candidate_id: str
    status: str
    record_id: str | None = None
    record_url: str | None = None
    message: str = ""
    observed_properties: dict[str, Any] = Field(default_factory=dict)


class AgentReport(StrictModel):
    request_id: str
    run_id: str
    generated_at: datetime = Field(default_factory=utc_now)
    requested_target: int
    target_reached: bool
    target_shortfall: int = Field(ge=0)
    discovered_candidates: int
    search_rounds: int = 0
    qualified: int
    rejected: int
    uncertain: int
    duplicates: int
    inactive: int
    pending_review: int
    imported: int
    import_failures: int
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
