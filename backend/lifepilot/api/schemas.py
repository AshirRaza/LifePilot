"""HTTP request/response models for the minimal agent API."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1, max_length=4000)
    jurisdiction: str = Field(..., min_length=2, max_length=8)
    pathway_id: str | None = None
    conversation_id: str | None = None
    # Production default is always auto; clients cannot disable verification via API.
    top_k: int = Field(default=8, ge=1, le=20)
    as_of_date: date | None = Field(
        default=None,
        description="Optional ISO legal reference date (YYYY-MM-DD).",
    )

    @field_validator("question")
    @classmethod
    def _strip_question(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("question must not be empty")
        return cleaned

    @field_validator("jurisdiction")
    @classmethod
    def _normalize_jurisdiction(cls, value: str) -> str:
        code = (value or "").strip().upper()
        if not code.replace("_", "").isalnum():
            raise ValueError("jurisdiction must be alphanumeric")
        return code

    @field_validator("pathway_id")
    @classmethod
    def _normalize_pathway(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class AnswerResponse(BaseModel):
    """HTTP answer payload.

    Additive Phase 10C.4 fields:
    - ``claim_verification``: per-retained-claim verification + evidence refs
    - ``evidence``: optional detailed/compact evidence list gated by
      ``LIFEPILOT_API_INCLUDE_EVIDENCE``
    Existing ``verification`` (full VerificationReport dump) is preserved.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    status: str
    answer: str | None = None
    claims: list[dict[str, Any]] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    retrieval_decision: str | None = None
    verification: dict[str, Any] | None = None
    claim_verification: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Per retained claim: status, reasons, uncertainty, and claim-bound "
            "evidence references. Rejected claims are omitted. Excerpts appear "
            "only when LIFEPILOT_API_INCLUDE_EVIDENCE is enabled."
        ),
    )
    timing: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] | None = None
    jurisdiction: str | None = None
    pathway_id: str | None = None
    as_of_date: date | None = None
    resolved_reference_date: date | None = None
    temporal_mode: str | None = None
    temporal_outcome: str | None = None
    evidence_count: int = 0
    evidence: list[dict[str, Any]] | None = None
    evidence_detail: Literal["none", "compact", "detailed"] = "none"
    legal_verification_policy: str | None = Field(
        default=None,
        description=(
            "Server-controlled policy (advisory | strict_verified_only). "
            "Clients cannot downgrade an enforced strict policy."
        ),
    )


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "lifepilot-api"


class ReadinessResponse(BaseModel):
    ready: bool
    details: dict[str, Any]


# --- Legal review governance (Phase 10D.2) ---


class LegalReviewAssignRequest(BaseModel):
    """Assign body. Client actor_id/actor_kind are forbidden (extra=forbid)."""

    model_config = ConfigDict(extra="forbid")

    assignee_actor_id: str = Field(..., min_length=1, max_length=200)
    expected_row_version: int = Field(..., ge=1)
    rationale: str | None = Field(default=None, max_length=4000)

    @field_validator("assignee_actor_id")
    @classmethod
    def _strip_assignee(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("assignee_actor_id must not be empty")
        return cleaned


class LegalReviewStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_row_version: int = Field(..., ge=1)
    rationale: str | None = Field(default=None, max_length=4000)


class LegalReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected", "changes_requested"]
    expected_row_version: int = Field(..., ge=1)
    rationale: str = Field(..., min_length=1, max_length=8000)
    document_version_id: int | None = Field(default=None, ge=1)
    evidence_fingerprint: str | None = Field(default=None, max_length=128)
    pathway_id: str | None = Field(default=None, max_length=200)
    requirement_category: str | None = Field(default=None, max_length=200)
    acknowledge_effective_period: bool = False
    acknowledge_cohort_conditions: bool = False

    @field_validator("rationale")
    @classmethod
    def _strip_rationale(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("rationale must not be empty")
        return cleaned


class LegalReviewReopenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_row_version: int = Field(..., ge=1)
    rationale: str = Field(..., min_length=1, max_length=8000)

    @field_validator("rationale")
    @classmethod
    def _strip_rationale(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("rationale must not be empty")
        return cleaned


class LegalReviewEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    event_type: str
    from_workflow_state: str | None
    to_workflow_state: str
    occurred_at: str
    actor_id: str
    actor_kind: str
    rationale: str | None = None
    document_version_id: int | None = None
    evidence_fingerprint: str | None = None


class LegalReviewItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    public_id: str
    subject_type: str
    subject_ref: str | None = None
    legal_rule_id: int | None = None
    jurisdiction_id: int
    pathway_id: str | None = None
    requirement_category: str | None = None
    risk_class: str
    workflow_state: str
    review_reason: str
    priority: str
    document_version_id: int | None = None
    evidence_fingerprint: str | None = None
    assigned_actor_id: str | None = None
    assigned_actor_kind: str | None = None
    invalidated: bool
    invalidation_reason: str | None = None
    current_approved_event_id: int | None = None
    row_version: int
    created_at: str
    updated_at: str
    events: list[LegalReviewEventResponse] | None = None


class LegalReviewListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[LegalReviewItemResponse]
    offset: int
    limit: int
    count: int
