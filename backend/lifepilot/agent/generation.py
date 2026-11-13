"""One-shot grounded answer generation using the configured LLM provider."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from lifepilot.agent.answer import (
    SYSTEM_PROMPT,
    GroundedAnswer,
    build_user_prompt,
    pack_evidence_context,
)
from lifepilot.agent.config import AgentConfig, get_agent_config
from lifepilot.agent.contracts import AgentStatus
from lifepilot.agent.errors import AgentExecutionError, LLMNotConfiguredError
from lifepilot.agent.evidence import EvidenceItem
from lifepilot.agent.grounding import GroundingResult, validate_and_ground_answer
from lifepilot.agent.providers import (
    LLMMessage,
    LLMProvider,
    LLMUsage,
    get_llm_provider,
)

logger = logging.getLogger(__name__)


@dataclass
class GenerationResult:
    status: AgentStatus
    answer: str | None = None
    claims: list[dict[str, Any]] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    usage: LLMUsage | None = None
    llm_ms: float = 0.0
    grounding_ms: float = 0.0
    model_id: str | None = None
    provider_id: str | None = None
    estimated_cost_usd: float | None = None


def estimate_cost_usd(usage: LLMUsage, config: AgentConfig) -> float | None:
    """Optional cost estimate when explicit pricing is configured."""
    if usage.input_tokens is None or usage.output_tokens is None:
        return None
    if config.llm_input_cost_per_mtok <= 0 and config.llm_output_cost_per_mtok <= 0:
        return None
    cost = (
        (usage.input_tokens / 1_000_000.0) * config.llm_input_cost_per_mtok
        + (usage.output_tokens / 1_000_000.0) * config.llm_output_cost_per_mtok
    )
    return round(cost, 6)


def generate_grounded_answer(
    *,
    question: str,
    jurisdiction: str,
    pathway_id: str | None,
    user_circumstances: dict[str, Any] | None,
    evidence: list[EvidenceItem],
    config: AgentConfig | None = None,
    provider: LLMProvider | None = None,
) -> GenerationResult:
    """Produce at most one structured Claude call, then validate citations."""
    cfg = config or get_agent_config()
    warnings: list[str] = []

    if not evidence:
        return GenerationResult(
            status=AgentStatus.INSUFFICIENT_EVIDENCE,
            missing_information=["retrieved_evidence"],
            warnings=["answer_skipped:no_evidence"],
        )

    llm = provider or get_llm_provider(cfg)

    packed = pack_evidence_context(
        evidence,
        max_items=min(cfg.max_evidence_items, cfg.llm_max_evidence_items),
        max_chars_per_passage=cfg.llm_max_chars_per_passage,
        max_total_chars=cfg.llm_max_context_chars,
    )
    if not packed:
        return GenerationResult(
            status=AgentStatus.INSUFFICIENT_EVIDENCE,
            missing_information=["retrieved_evidence"],
            warnings=["answer_skipped:empty_evidence_context"],
        )

    user_prompt = build_user_prompt(
        question=question,
        jurisdiction=jurisdiction,
        pathway_id=pathway_id,
        user_circumstances=user_circumstances,
        evidence_payload=packed,
    )
    messages = [
        LLMMessage(role="system", content=SYSTEM_PROMPT),
        LLMMessage(role="user", content=user_prompt),
    ]

    try:
        structured = llm.complete_structured(
            messages,
            schema=GroundedAnswer,
            max_tokens=cfg.max_llm_output_tokens,
            temperature=0.0,
        )
    except LLMNotConfiguredError:
        return GenerationResult(
            status=AgentStatus.SUCCESS,
            answer=None,
            warnings=["answer_skipped:llm_not_configured"],
            missing_information=[],
        )
    except AgentExecutionError as exc:
        logger.exception("agent.llm_execution_error")
        return GenerationResult(
            status=AgentStatus.EXECUTION_FAILURE,
            errors=[exc.message],
            warnings=warnings,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("agent.llm_unexpected_error")
        return GenerationResult(
            status=AgentStatus.EXECUTION_FAILURE,
            errors=[f"llm_failed:{exc.__class__.__name__}"],
            warnings=warnings,
        )

    g_started = time.perf_counter()
    grounded: GroundingResult = validate_and_ground_answer(
        structured.value,  # type: ignore[arg-type]
        evidence=evidence,
        jurisdiction=jurisdiction,
    )
    grounding_ms = (time.perf_counter() - g_started) * 1000
    warnings.extend(grounded.warnings)

    if grounded.answer is None:
        return GenerationResult(
            status=AgentStatus.INSUFFICIENT_EVIDENCE,
            missing_information=["supported_claims"],
            warnings=warnings + grounded.errors,
            usage=structured.usage,
            llm_ms=structured.latency_ms,
            grounding_ms=round(grounding_ms, 3),
            model_id=structured.model,
            provider_id=structured.provider,
            estimated_cost_usd=estimate_cost_usd(structured.usage, cfg),
        )

    status = AgentStatus.SUCCESS
    if grounded.status_hint == "insufficient_evidence":
        status = AgentStatus.INSUFFICIENT_EVIDENCE

    answer_obj = grounded.answer
    # Surface model limitations as warnings (user-visible).
    for lim in answer_obj.limitations:
        if lim and lim not in warnings:
            warnings.append(lim)

    return GenerationResult(
        status=status,
        answer=answer_obj.answer,
        claims=[c.model_dump(mode="json") for c in answer_obj.claims],
        missing_information=list(answer_obj.missing_information),
        warnings=warnings,
        errors=list(grounded.errors),
        usage=structured.usage,
        llm_ms=structured.latency_ms,
        grounding_ms=round(grounding_ms, 3),
        model_id=structured.model,
        provider_id=structured.provider,
        estimated_cost_usd=estimate_cost_usd(structured.usage, cfg),
    )
