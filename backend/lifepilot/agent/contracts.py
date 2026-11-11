"""Typed agent request/response contracts."""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lifepilot.agent.config import AgentConfig, get_agent_config
from lifepilot.agent.errors import AgentValidationError
from lifepilot.agent.evidence import EvidenceItem, RetrievalModeOption


class AgentStatus(str, Enum):
    SUCCESS = "success"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    EXECUTION_FAILURE = "execution_failure"
    INVALID_REQUEST = "invalid_request"


class RetrievalOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: RetrievalModeOption = "hybrid"
    top_k: int = Field(default=8, ge=1, le=50)
    include_external: bool = False


class AgentRequest(BaseModel):
    """Validated inbound agent request.

    ``pathway_id`` is treated as a user hint only — never as proven legal
    applicability. ``conversation_id`` is an opaque correlation key and does
    not confer authorization.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1)
    jurisdiction: str = Field(..., min_length=2, max_length=8)
    pathway_id: str | None = None
    user_circumstances: dict[str, Any] | None = None
    conversation_id: str | None = Field(
        default=None,
        description=(
            "Optional opaque conversation correlation id. Not an auth token; "
            "authenticated isolation is deferred to later phases."
        ),
    )
    retrieval: RetrievalOptions = Field(default_factory=RetrievalOptions)
    request_id: str | None = None

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
        if not code:
            raise ValueError("jurisdiction is required")
        if not code.replace("_", "").isalnum():
            raise ValueError("jurisdiction must be an alphanumeric code")
        return code

    @field_validator("pathway_id")
    @classmethod
    def _normalize_pathway(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("conversation_id")
    @classmethod
    def _normalize_conversation(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    def ensure_request_id(self) -> str:
        if self.request_id:
            return self.request_id
        return f"req_{uuid.uuid4().hex}"


class TimingMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_ms: float = 0.0
    validate_ms: float | None = None
    evidence_ms: float | None = None
    answer_ms: float | None = None
    internal_retrieval_ms: float | None = None
    external_retrieval_ms: float | None = None
    normalize_ms: float | None = None


class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    status: AgentStatus
    evidence: list[EvidenceItem] = Field(default_factory=list)
    answer: str | None = None
    missing_information: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    timing: TimingMetadata = Field(default_factory=TimingMetadata)
    jurisdiction: str | None = None
    pathway_id: str | None = None
    conversation_id: str | None = None


def validate_agent_request(
    request: AgentRequest,
    *,
    config: AgentConfig | None = None,
) -> AgentRequest:
    """Apply runtime limits that Pydantic field bounds alone do not cover."""
    cfg = config or get_agent_config()
    if len(request.question) > cfg.max_question_length:
        raise AgentValidationError(
            f"question exceeds max length ({cfg.max_question_length} characters)",
            code="question_too_long",
        )
    if request.retrieval.top_k > cfg.max_retrieval_top_k:
        raise AgentValidationError(
            f"retrieval.top_k exceeds configured maximum ({cfg.max_retrieval_top_k})",
            code="top_k_too_large",
        )
    return request
