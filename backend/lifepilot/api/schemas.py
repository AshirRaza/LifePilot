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


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "lifepilot-api"


class ReadinessResponse(BaseModel):
    ready: bool
    details: dict[str, Any]
