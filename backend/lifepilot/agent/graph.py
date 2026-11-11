"""Minimal compilable LangGraph workflow for Phase 9A.

Nodes:
  validate_initialize → evidence_extension_point → answer_extension_point → finalize

Evidence retrieval and answer generation are explicit extension points that do
not call LLMs, RAG, or external APIs in this phase.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any

from langgraph.graph import END, START, StateGraph

from lifepilot.agent.config import AgentConfig, get_agent_config
from lifepilot.agent.contracts import AgentStatus
from lifepilot.agent.state import AgentGraphState

logger = logging.getLogger(__name__)

# Fixed graph topology — no unbounded loops or retries in Phase 9A.
GRAPH_NODE_ORDER = (
    "validate_initialize",
    "evidence_extension_point",
    "answer_extension_point",
    "finalize",
)


def _bump_step(state: AgentGraphState) -> int:
    return int(state.get("step_count", 0)) + 1


def validate_initialize(state: AgentGraphState) -> dict[str, Any]:
    """Deterministic validation / initialization node."""
    started = time.perf_counter()
    errors = list(state.get("errors") or [])
    warnings = list(state.get("warnings") or [])
    status = state.get("status") or AgentStatus.SUCCESS.value
    step = _bump_step(state)
    max_steps = int(state.get("max_workflow_steps") or 16)

    if step > max_steps:
        errors.append("max_workflow_steps exceeded during validate_initialize")
        status = AgentStatus.EXECUTION_FAILURE.value

    question = (state.get("question") or "").strip()
    jurisdiction = (state.get("jurisdiction") or "").strip().upper()
    if not question:
        errors.append("question is required")
        status = AgentStatus.INVALID_REQUEST.value
    if not jurisdiction:
        errors.append("jurisdiction is required")
        status = AgentStatus.INVALID_REQUEST.value

    # Pathway is a user hint only — never treat as proven applicability.
    if state.get("pathway_id"):
        warnings.append(
            "pathway_id is a user-provided hint only and does not establish "
            "legal applicability or COMPLETE coverage."
        )

    if state.get("conversation_id"):
        warnings.append(
            "conversation_id is an opaque correlation key and does not confer "
            "authorization or cross-request isolation by itself."
        )

    warnings.append(
        "Phase 9A foundation run: evidence orchestration and answer generation "
        "are deferred extension points."
    )

    elapsed = (time.perf_counter() - started) * 1000
    logger.info(
        "agent.validate request_id=%s jurisdiction=%s status=%s",
        state.get("request_id"),
        jurisdiction or None,
        status,
    )
    return {
        "step_count": step,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "jurisdiction": jurisdiction,
        "validate_ms": round(elapsed, 3),
    }


def evidence_extension_point(state: AgentGraphState) -> dict[str, Any]:
    """Placeholder for Phase 9B evidence orchestration (no network)."""
    started = time.perf_counter()
    step = _bump_step(state)
    max_steps = int(state.get("max_workflow_steps") or 16)
    errors = list(state.get("errors") or [])
    warnings = list(state.get("warnings") or [])
    deferred = list(state.get("deferred_stages") or [])
    status = state.get("status") or AgentStatus.SUCCESS.value

    missing = list(state.get("missing_information") or [])

    if status in {
        AgentStatus.INVALID_REQUEST.value,
        AgentStatus.EXECUTION_FAILURE.value,
        AgentStatus.UNSUPPORTED_OPERATION.value,
    }:
        return {
            "step_count": step,
            "evidence_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    if step > max_steps:
        errors.append("max_workflow_steps exceeded during evidence_extension_point")
        status = AgentStatus.EXECUTION_FAILURE.value
    else:
        deferred.append("evidence_orchestration")
        warnings.append(
            "evidence_extension_point skipped live retrieval "
            "(internal RAG + external connectors deferred to Phase 9B)."
        )
        # Empty evidence is expected in 9A; later phases may set
        # insufficient_evidence when orchestration finds nothing usable.
        if "retrieved_evidence" not in missing:
            missing.append("retrieved_evidence")

    return {
        "step_count": step,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "deferred_stages": deferred,
        "evidence": list(state.get("evidence") or []),
        "missing_information": missing,
        "evidence_ms": round((time.perf_counter() - started) * 1000, 3),
    }

def answer_extension_point(state: AgentGraphState) -> dict[str, Any]:
    """Placeholder for future answer generation (no LLM calls)."""
    started = time.perf_counter()
    step = _bump_step(state)
    max_steps = int(state.get("max_workflow_steps") or 16)
    errors = list(state.get("errors") or [])
    warnings = list(state.get("warnings") or [])
    deferred = list(state.get("deferred_stages") or [])
    status = state.get("status") or AgentStatus.SUCCESS.value

    if status in {
        AgentStatus.INVALID_REQUEST.value,
        AgentStatus.EXECUTION_FAILURE.value,
        AgentStatus.UNSUPPORTED_OPERATION.value,
    }:
        return {
            "step_count": step,
            "answer_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    if step > max_steps:
        errors.append("max_workflow_steps exceeded during answer_extension_point")
        status = AgentStatus.EXECUTION_FAILURE.value
    else:
        deferred.append("answer_generation")
        warnings.append(
            "answer_extension_point skipped LLM generation "
            "(no provider calls in Phase 9A)."
        )

    return {
        "step_count": step,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "deferred_stages": deferred,
        "answer": None,
        "answer_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def finalize(state: AgentGraphState) -> dict[str, Any]:
    """Terminal node — produce structured completion metadata."""
    step = _bump_step(state)
    total = (
        float(state.get("validate_ms") or 0.0)
        + float(state.get("evidence_ms") or 0.0)
        + float(state.get("answer_ms") or 0.0)
    )
    status = state.get("status") or AgentStatus.SUCCESS.value
    if state.get("errors") and status == AgentStatus.SUCCESS.value:
        status = AgentStatus.EXECUTION_FAILURE.value

    logger.info(
        "agent.finalize request_id=%s status=%s steps=%s evidence=%s",
        state.get("request_id"),
        status,
        step,
        len(state.get("evidence") or []),
    )
    return {
        "step_count": step,
        "status": status,
        "total_ms": round(total, 3),
    }


def build_agent_graph(config: AgentConfig | None = None) -> Any:
    """Construct an uncompiled StateGraph with Phase 9A topology."""
    _ = config or get_agent_config()
    graph: StateGraph = StateGraph(AgentGraphState)
    graph.add_node("validate_initialize", validate_initialize)
    graph.add_node("evidence_extension_point", evidence_extension_point)
    graph.add_node("answer_extension_point", answer_extension_point)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "validate_initialize")
    graph.add_edge("validate_initialize", "evidence_extension_point")
    graph.add_edge("evidence_extension_point", "answer_extension_point")
    graph.add_edge("answer_extension_point", "finalize")
    graph.add_edge("finalize", END)
    return graph


@lru_cache(maxsize=1)
def get_compiled_agent_graph() -> Any:
    """Compile the foundation graph without a checkpointer (no DB dependency)."""
    return build_agent_graph().compile()


def reset_compiled_agent_graph() -> None:
    """Drop cached compiled graph (tests)."""
    get_compiled_agent_graph.cache_clear()
