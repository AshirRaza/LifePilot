"""Deterministic agent runner for evidence orchestration and grounded answers."""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import ValidationError

from lifepilot.agent.answer import AnswerClaim
from lifepilot.agent.config import AgentConfig, get_agent_config
from lifepilot.agent.contracts import (
    AgentRequest,
    AgentResponse,
    AgentStatus,
    TimingMetadata,
    UsageMetadata,
    validate_agent_request,
)
from lifepilot.agent.errors import AgentError, AgentValidationError
from lifepilot.agent.evidence import EvidenceItem
from lifepilot.agent.graph import get_compiled_agent_graph
from lifepilot.agent.state import initial_state_from_request

logger = logging.getLogger(__name__)


def _response_from_state(
    state: dict[str, Any],
    *,
    request: AgentRequest,
    wall_ms: float,
) -> AgentResponse:
    evidence_raw = state.get("evidence") or []
    evidence: list[EvidenceItem] = []
    for item in evidence_raw:
        if isinstance(item, EvidenceItem):
            evidence.append(item)
        elif isinstance(item, dict):
            evidence.append(EvidenceItem.model_validate(item))

    claims_raw = state.get("claims") or []
    claims: list[AnswerClaim] = []
    for item in claims_raw:
        if isinstance(item, AnswerClaim):
            claims.append(item)
        elif isinstance(item, dict):
            claims.append(AnswerClaim.model_validate(item))

    status_raw = state.get("status") or AgentStatus.SUCCESS.value
    try:
        status = AgentStatus(status_raw)
    except ValueError:
        status = AgentStatus.EXECUTION_FAILURE

    usage = None
    usage_raw = state.get("usage")
    if isinstance(usage_raw, UsageMetadata):
        usage = usage_raw
    elif isinstance(usage_raw, dict):
        usage = UsageMetadata.model_validate(usage_raw)

    return AgentResponse(
        request_id=state.get("request_id") or request.ensure_request_id(),
        status=status,
        evidence=evidence,
        answer=state.get("answer"),
        claims=claims,
        missing_information=list(state.get("missing_information") or []),
        warnings=list(state.get("warnings") or []),
        errors=list(state.get("errors") or []),
        timing=TimingMetadata(
            total_ms=round(float(state.get("total_ms") or wall_ms), 3),
            validate_ms=state.get("validate_ms"),
            evidence_ms=state.get("evidence_ms"),
            answer_ms=state.get("answer_ms"),
            internal_retrieval_ms=state.get("internal_retrieval_ms"),
            external_retrieval_ms=state.get("external_retrieval_ms"),
            normalize_ms=state.get("normalize_ms"),
            llm_ms=state.get("llm_ms"),
            grounding_ms=state.get("grounding_ms"),
        ),
        usage=usage,
        jurisdiction=state.get("jurisdiction") or request.jurisdiction,
        pathway_id=state.get("pathway_id") or request.pathway_id,
        conversation_id=state.get("conversation_id") or request.conversation_id,
    )


def run_agent(
    request: AgentRequest | dict[str, Any],
    *,
    config: AgentConfig | None = None,
) -> AgentResponse:
    """Execute the agent graph for a single request.

    Each call builds a fresh state dict — requests cannot share mutable state.
    LLM calls occur only when evidence is available and Anthropic is configured.
    """
    started = time.perf_counter()
    cfg = config or get_agent_config()

    try:
        if isinstance(request, dict):
            parsed = AgentRequest.model_validate(request)
        else:
            parsed = request
        parsed = validate_agent_request(parsed, config=cfg)
    except ValidationError as exc:
        wall = (time.perf_counter() - started) * 1000
        return AgentResponse(
            request_id=f"req_invalid_{int(started * 1000)}",
            status=AgentStatus.INVALID_REQUEST,
            errors=[str(exc)],
            timing=TimingMetadata(total_ms=round(wall, 3)),
        )
    except AgentValidationError as exc:
        wall = (time.perf_counter() - started) * 1000
        req_id = (
            request.ensure_request_id()
            if isinstance(request, AgentRequest)
            else f"req_invalid_{int(started * 1000)}"
        )
        return AgentResponse(
            request_id=req_id,
            status=AgentStatus.INVALID_REQUEST,
            errors=[exc.message],
            timing=TimingMetadata(total_ms=round(wall, 3)),
            jurisdiction=getattr(request, "jurisdiction", None)
            if isinstance(request, AgentRequest)
            else None,
        )

    request_id = parsed.ensure_request_id()
    if not parsed.request_id:
        parsed = parsed.model_copy(update={"request_id": request_id})

    initial = initial_state_from_request(
        request_id=request_id,
        question=parsed.question,
        jurisdiction=parsed.jurisdiction,
        pathway_id=parsed.pathway_id,
        user_circumstances=parsed.user_circumstances,
        conversation_id=parsed.conversation_id,
        retrieval_mode=parsed.retrieval.mode,
        retrieval_top_k=parsed.retrieval.top_k,
        include_external=parsed.retrieval.include_external,
        max_workflow_steps=cfg.max_workflow_steps,
    )

    try:
        graph = get_compiled_agent_graph()
        final_state = graph.invoke(initial)
        wall = (time.perf_counter() - started) * 1000
        if isinstance(final_state, dict) and not final_state.get("total_ms"):
            final_state = {**final_state, "total_ms": round(wall, 3)}
        return _response_from_state(final_state, request=parsed, wall_ms=wall)
    except AgentError as exc:
        wall = (time.perf_counter() - started) * 1000
        logger.exception(
            "agent.execution_error request_id=%s code=%s", request_id, exc.code
        )
        return AgentResponse(
            request_id=request_id,
            status=AgentStatus.EXECUTION_FAILURE,
            errors=[exc.message],
            timing=TimingMetadata(total_ms=round(wall, 3)),
            jurisdiction=parsed.jurisdiction,
            pathway_id=parsed.pathway_id,
            conversation_id=parsed.conversation_id,
        )
    except Exception as exc:  # noqa: BLE001
        wall = (time.perf_counter() - started) * 1000
        logger.exception("agent.unexpected_error request_id=%s", request_id)
        return AgentResponse(
            request_id=request_id,
            status=AgentStatus.EXECUTION_FAILURE,
            errors=[f"unexpected execution failure: {exc.__class__.__name__}"],
            timing=TimingMetadata(total_ms=round(wall, 3)),
            jurisdiction=parsed.jurisdiction,
            pathway_id=parsed.pathway_id,
            conversation_id=parsed.conversation_id,
        )
