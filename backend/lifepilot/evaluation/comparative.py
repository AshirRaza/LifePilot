"""Comparative answer evaluation helpers (Phase 9D.1).

Evaluation-only adapters. Does not change production agent defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lifepilot.agent.evidence import EvidenceSourceType
from lifepilot.agent.orchestration import (
    EvidenceOrchestrator,
    OrchestrationResult,
    get_evidence_orchestrator,
)
from lifepilot.agent.contracts import AgentStatus


MODES = ("internal_only", "external_only", "auto_hybrid")


@dataclass
class ModeSpec:
    mode_id: str
    external_mode: str | None
    include_external: bool | None
    external_only_filter: bool = False


MODE_SPECS: dict[str, ModeSpec] = {
    "internal_only": ModeSpec(
        mode_id="internal_only",
        external_mode="off",
        include_external=False,
    ),
    "external_only": ModeSpec(
        mode_id="external_only",
        external_mode="on",
        include_external=True,
        external_only_filter=True,
    ),
    "auto_hybrid": ModeSpec(
        mode_id="auto_hybrid",
        external_mode="auto",
        include_external=None,
    ),
}


class ExternalOnlyEvidenceOrchestrator:
    """Eval-only wrapper: force connectors, drop internal RAG passages."""

    def __init__(self, inner: EvidenceOrchestrator | None = None) -> None:
        self._inner = inner or get_evidence_orchestrator()

    def gather(self, **kwargs: Any) -> OrchestrationResult:
        kwargs["external_mode"] = "on"
        kwargs["include_external"] = True
        result = self._inner.gather(**kwargs)
        filtered = [
            e
            for e in result.evidence
            if e.source_type == EvidenceSourceType.EXTERNAL_API
        ]
        result.evidence = filtered
        result.retrieval_decision = "external_only_diagnostic"
        reasons = list(result.retrieval_decision_reasons or [])
        reasons.append("eval_adapter:stripped_internal_rag")
        result.retrieval_decision_reasons = reasons
        if not filtered:
            # Preserve usable warnings; mark insufficient if nothing left.
            if result.status == AgentStatus.SUCCESS:
                result.status = AgentStatus.INSUFFICIENT_EVIDENCE
                missing = list(result.missing_information or [])
                if "retrieved_evidence" not in missing:
                    missing.append("retrieved_evidence")
                result.missing_information = missing
            result.warnings = list(result.warnings or []) + [
                "external_only_diagnostic:no_external_passages_after_filter"
            ]
        return result

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()


def retrieval_options_for_mode(mode_id: str) -> dict[str, Any]:
    spec = MODE_SPECS[mode_id]
    return {
        "mode": "hybrid",
        "top_k": 8,
        "external_mode": spec.external_mode or "auto",
        "include_external": spec.include_external,
    }


def classify_automated_assessment(
    *,
    case: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, Any]:
    """Deterministic automated layer — never claims independent legal accuracy."""
    answer = (row.get("answer") or "").lower()
    warnings = [str(w) for w in (row.get("warnings") or [])]
    missing = [str(m) for m in (row.get("missing_information") or [])]
    claims = row.get("claims") or []
    evidence = row.get("evidence") or []
    status = row.get("agent_status") or row.get("status")

    personal_prohibited = bool(case.get("personal_eligibility_prohibited"))
    expected = (case.get("expected_behavior") or "").lower()
    risk = (case.get("risk_category") or "").lower()

    eligibility_leak = bool(
        personal_prohibited
        and any(
            p in answer
            for p in (
                "you are eligible",
                "you qualify",
                "definitely eligible",
                "you personally qualify",
                "confirmed that you qualify",
            )
        )
    )

    citation_ids = {
        e.get("evidence_id") for e in evidence if e.get("evidence_id")
    }
    invalid_cites = []
    for c in claims:
        for eid in c.get("evidence_ids") or []:
            if eid not in citation_ids:
                invalid_cites.append(eid)

    abstained = status in {"insufficient_evidence"} or (
        not (row.get("answer") or "").strip()
    ) or any(
        x in answer
        for x in (
            "cannot confirm",
            "not enough evidence",
            "insufficient evidence",
            "cannot determine",
            "would need",
            "still need",
            "missing information",
        )
    )

    pathway_warning = any("pathway" in w.lower() for w in warnings)
    numeric_conflict = any("numeric_conflict" in w for w in warnings)
    future_effective = any("future_effective" in w for w in warnings)

    factual_label = "unverifiable_from_available_reference_evidence"
    if eligibility_leak or invalid_cites:
        factual_label = "incorrect_or_contradicted_heuristic"
    elif numeric_conflict:
        factual_label = "partially_correct_or_incomplete"
    elif status == "insufficient_evidence" and "must_abstain" in expected:
        factual_label = "correct_and_supported"  # appropriate abstention support
    elif not evidence and not abstained and (row.get("answer") or "").strip():
        factual_label = "incorrect_or_contradicted_heuristic"

    return {
        "factual_legal_correctness_automated": factual_label,
        "factual_independently_verified": False,
        "completeness_automated": (
            "incomplete_or_unknown"
            if missing or not claims
            else "appears_structured"
        ),
        "citation_quality": {
            "claims": len(claims),
            "invalid_citation_ids": sorted(set(invalid_cites)),
            "ok": not invalid_cites,
        },
        "temporal_flags": {
            "future_effective_warning": future_effective,
            "numeric_conflict_warning": numeric_conflict,
        },
        "safety": {
            "personal_eligibility_prohibited": personal_prohibited,
            "eligibility_leak_heuristic": eligibility_leak,
            "appropriate_abstention_heuristic": bool(
                abstained and ("abstain" in expected or personal_prohibited)
            ),
            "pathway_warning_present": pathway_warning,
        },
        "risk_category": risk,
        "human_review_required": True,
        "human_verdict": "not_yet_verified",
    }
