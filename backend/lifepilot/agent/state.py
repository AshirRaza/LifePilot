"""LangGraph workflow state for the immigration agent foundation.

User-provided facts stay separate from verified/retrieved evidence. Conversation
identifiers are opaque correlation keys only — they do not authorize access.
Persistent PostgreSQL checkpointing is a planned later-phase boundary and is
intentionally not enabled here.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class AgentGraphState(TypedDict):
    """LangGraph-compatible workflow state.

    Designed for future multi-turn use without mixing users or conversations.
    Sensitive profile data must not be stored in process-global state.
    """

    # Correlation / isolation keys (opaque; not auth).
    request_id: str
    conversation_id: str | None

    # User-provided inputs (not treated as verified evidence).
    question: str
    jurisdiction: str
    pathway_id: str | None
    user_circumstances: dict[str, Any] | None
    retrieval_mode: str
    retrieval_top_k: int
    include_external: bool

    # Execution control.
    step_count: int
    max_workflow_steps: int
    status: str
    errors: list[str]
    warnings: list[str]
    missing_information: list[str]

    # Evidence vs answer.
    evidence: list[dict[str, Any]]
    answer: str | None
    claims: NotRequired[list[dict[str, Any]]]

    # Timing (milliseconds).
    validate_ms: float
    evidence_ms: float
    answer_ms: float
    total_ms: float
    internal_retrieval_ms: NotRequired[float]
    external_retrieval_ms: NotRequired[float]
    normalize_ms: NotRequired[float]
    llm_ms: NotRequired[float]
    grounding_ms: NotRequired[float]

    # Extension bookkeeping.
    deferred_stages: NotRequired[list[str]]
    external_provider_outcomes: NotRequired[list[dict[str, Any]]]
    usage: NotRequired[dict[str, Any] | None]


def initial_state_from_request(
    *,
    request_id: str,
    question: str,
    jurisdiction: str,
    pathway_id: str | None,
    user_circumstances: dict[str, Any] | None,
    conversation_id: str | None,
    retrieval_mode: str,
    retrieval_top_k: int,
    include_external: bool,
    max_workflow_steps: int,
) -> AgentGraphState:
    """Build an isolated per-request state dict (no shared globals)."""
    return AgentGraphState(
        request_id=request_id,
        conversation_id=conversation_id,
        question=question,
        jurisdiction=jurisdiction,
        pathway_id=pathway_id,
        user_circumstances=user_circumstances,
        retrieval_mode=retrieval_mode,
        retrieval_top_k=retrieval_top_k,
        include_external=include_external,
        step_count=0,
        max_workflow_steps=max_workflow_steps,
        status="success",
        errors=[],
        warnings=[],
        missing_information=[],
        evidence=[],
        answer=None,
        claims=[],
        validate_ms=0.0,
        evidence_ms=0.0,
        answer_ms=0.0,
        total_ms=0.0,
        deferred_stages=[],
        usage=None,
    )


# Future checkpointing boundary:
# - Prefer a PostgreSQL-backed LangGraph checkpointer scoped by
#   (authenticated_user_id, conversation_id).
# - Do not enable checkpointers in unit tests.
# - Accepting conversation_id alone must never grant cross-user access.
CHECKPOINTING_BOUNDARY_NOTE = (
    "PostgreSQL LangGraph checkpointing is deferred. Compile graphs without a "
    "checkpointer until authenticated isolation is implemented."
)
