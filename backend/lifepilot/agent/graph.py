"""LangGraph workflow: validate → evidence → grounded answer → finalize."""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from lifepilot.agent.config import AgentConfig, get_agent_config
from lifepilot.agent.contracts import AgentStatus, UsageMetadata
from lifepilot.agent.evidence import EvidenceItem
from lifepilot.agent.generation import generate_grounded_answer
from lifepilot.agent.orchestration import get_evidence_orchestrator
from lifepilot.agent.providers import get_llm_provider
from lifepilot.agent.state import AgentGraphState

logger = logging.getLogger(__name__)

# Fixed graph topology — no unbounded loops or tool-calling agents.
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
    """Orchestrate internal hybrid RAG + optional external connectors."""
    started = time.perf_counter()
    step = _bump_step(state)
    max_steps = int(state.get("max_workflow_steps") or 16)
    errors = list(state.get("errors") or [])
    warnings = list(state.get("warnings") or [])
    deferred = list(state.get("deferred_stages") or [])
    status = state.get("status") or AgentStatus.SUCCESS.value
    missing = list(state.get("missing_information") or [])
    cfg = get_agent_config()

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
        return {
            "step_count": step,
            "status": AgentStatus.EXECUTION_FAILURE.value,
            "errors": errors,
            "evidence_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    validate_ms = float(state.get("validate_ms") or 0.0)
    deadline = time.perf_counter() + max(
        0.1, float(cfg.request_timeout_seconds) - (validate_ms / 1000.0)
    )

    orchestrator = get_evidence_orchestrator(cfg)
    try:
        result = orchestrator.gather(
            question=str(state.get("question") or ""),
            jurisdiction=str(state.get("jurisdiction") or ""),
            pathway_id=state.get("pathway_id"),
            retrieval_mode=str(state.get("retrieval_mode") or "hybrid"),
            top_k=int(state.get("retrieval_top_k") or cfg.default_retrieval_top_k),
            include_external=bool(state.get("include_external")),
            request_id=state.get("request_id"),
            deadline_monotonic=deadline,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "agent.evidence_orchestration_error request_id=%s", state.get("request_id")
        )
        return {
            "step_count": step,
            "status": AgentStatus.EXECUTION_FAILURE.value,
            "errors": errors + [f"evidence_orchestration_failed:{exc.__class__.__name__}"],
            "warnings": warnings,
            "evidence": [],
            "missing_information": missing + ["retrieved_evidence"],
            "evidence_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    warnings.extend(result.warnings)
    errors.extend(result.errors)
    missing = list(result.missing_information)
    status = result.status.value

    return {
        "step_count": step,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "deferred_stages": deferred,
        "evidence": [item.model_dump(mode="json") for item in result.evidence],
        "missing_information": missing,
        "evidence_ms": round((time.perf_counter() - started) * 1000, 3),
        "internal_retrieval_ms": result.internal_ms,
        "external_retrieval_ms": result.external_ms,
        "normalize_ms": result.normalize_ms,
        "external_provider_outcomes": result.external_provider_outcomes,
    }


def _should_generate_answer(state: AgentGraphState) -> bool:
    status = state.get("status") or AgentStatus.SUCCESS.value
    if status != AgentStatus.SUCCESS.value:
        return False
    if state.get("errors"):
        return False
    evidence = state.get("evidence") or []
    return bool(evidence)


def route_after_evidence(
    state: AgentGraphState,
) -> Literal["answer_extension_point", "finalize"]:
    """Skip LLM when evidence is insufficient or the request already failed."""
    if _should_generate_answer(state):
        return "answer_extension_point"
    return "finalize"


def answer_extension_point(state: AgentGraphState) -> dict[str, Any]:
    """One Claude structured generation call + citation grounding."""
    started = time.perf_counter()
    step = _bump_step(state)
    max_steps = int(state.get("max_workflow_steps") or 16)
    errors = list(state.get("errors") or [])
    warnings = list(state.get("warnings") or [])
    missing = list(state.get("missing_information") or [])
    deferred = list(state.get("deferred_stages") or [])
    status = state.get("status") or AgentStatus.SUCCESS.value
    cfg = get_agent_config()

    if not _should_generate_answer(state):
        deferred.append("answer_generation")
        warnings.append("answer_skipped:insufficient_or_failed_evidence")
        return {
            "step_count": step,
            "status": status,
            "warnings": warnings,
            "deferred_stages": deferred,
            "answer": None,
            "claims": [],
            "answer_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    if step > max_steps:
        errors.append("max_workflow_steps exceeded during answer_extension_point")
        return {
            "step_count": step,
            "status": AgentStatus.EXECUTION_FAILURE.value,
            "errors": errors,
            "answer_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    evidence_items: list[EvidenceItem] = []
    for raw in state.get("evidence") or []:
        if isinstance(raw, EvidenceItem):
            evidence_items.append(raw)
        elif isinstance(raw, dict):
            evidence_items.append(EvidenceItem.model_validate(raw))

    # Overall deadline remaining for the LLM call.
    spent = (
        float(state.get("validate_ms") or 0.0)
        + float(state.get("evidence_ms") or 0.0)
    ) / 1000.0
    remaining = max(1.0, float(cfg.request_timeout_seconds) - spent)
    if remaining < 1.0:
        return {
            "step_count": step,
            "status": AgentStatus.EXECUTION_FAILURE.value,
            "errors": errors + ["request_deadline_exceeded_before_llm"],
            "answer_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    result = generate_grounded_answer(
        question=str(state.get("question") or ""),
        jurisdiction=str(state.get("jurisdiction") or ""),
        pathway_id=state.get("pathway_id"),
        user_circumstances=state.get("user_circumstances"),
        evidence=evidence_items,
        config=cfg,
        provider=get_llm_provider(cfg),
    )

    warnings.extend(result.warnings)
    errors.extend(result.errors)
    if result.missing_information:
        # Merge without duplicates.
        for item in result.missing_information:
            if item not in missing:
                missing.append(item)

    usage = UsageMetadata.from_llm_usage(
        result.usage,
        provider=result.provider_id,
        model=result.model_id,
        estimated_cost_usd=result.estimated_cost_usd,
    )

    # Preserve evidence success unless generation/grounding demotes status.
    out_status = result.status.value
    if (
        out_status == AgentStatus.SUCCESS.value
        and status == AgentStatus.SUCCESS.value
        and result.answer is None
        and any("llm_not_configured" in w for w in result.warnings)
    ):
        deferred.append("answer_generation")

    return {
        "step_count": step,
        "status": out_status,
        "errors": errors,
        "warnings": warnings,
        "deferred_stages": deferred,
        "missing_information": missing,
        "answer": result.answer,
        "claims": result.claims,
        "answer_ms": round((time.perf_counter() - started) * 1000, 3),
        "llm_ms": result.llm_ms,
        "grounding_ms": result.grounding_ms,
        "usage": None if usage is None else usage.model_dump(mode="json"),
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
    if (
        state.get("errors")
        and status == AgentStatus.SUCCESS.value
        and not (state.get("evidence") or [])
        and not state.get("answer")
    ):
        status = AgentStatus.EXECUTION_FAILURE.value

    logger.info(
        "agent.finalize request_id=%s status=%s steps=%s evidence=%s has_answer=%s",
        state.get("request_id"),
        status,
        step,
        len(state.get("evidence") or []),
        bool(state.get("answer")),
    )
    return {
        "step_count": step,
        "status": status,
        "total_ms": round(total, 3),
    }


def build_agent_graph(config: AgentConfig | None = None) -> Any:
    """Construct an uncompiled StateGraph with conditional LLM routing."""
    _ = config or get_agent_config()
    graph: StateGraph = StateGraph(AgentGraphState)
    graph.add_node("validate_initialize", validate_initialize)
    graph.add_node("evidence_extension_point", evidence_extension_point)
    graph.add_node("answer_extension_point", answer_extension_point)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "validate_initialize")
    graph.add_edge("validate_initialize", "evidence_extension_point")
    graph.add_conditional_edges(
        "evidence_extension_point",
        route_after_evidence,
        {
            "answer_extension_point": "answer_extension_point",
            "finalize": "finalize",
        },
    )
    graph.add_edge("answer_extension_point", "finalize")
    graph.add_edge("finalize", END)
    return graph


@lru_cache(maxsize=1)
def get_compiled_agent_graph() -> Any:
    """Compile the graph without a checkpointer (no DB dependency)."""
    return build_agent_graph().compile()


def reset_compiled_agent_graph() -> None:
    """Drop cached compiled graph (tests)."""
    get_compiled_agent_graph.cache_clear()
