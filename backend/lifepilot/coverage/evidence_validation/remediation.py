"""Prioritized remediation plan grounded in existing repository URLs only."""

from __future__ import annotations

from typing import Any

from lifepilot.coverage.evidence_validation.decisions import FieldDecision
from lifepilot.coverage.evidence_validation.readiness import recalculate_readiness

IMPACT_ORDER = {
    "correct_evidence_selection": 0,
    "reprocess_extraction": 1,
    "inspect_existing_document": 2,
    "discover_missing_page": 3,
    "propose_allowlist_expansion": 4,
    "human_verification": 5,
}


def build_remediation_plan(
    decisions: list[FieldDecision],
    *,
    grounded_urls: list[str] | None = None,
) -> dict[str, Any]:
    readiness = recalculate_readiness(decisions)
    actions: list[dict[str, Any]] = []

    # High-impact: downgraded ready pathways first
    for item in readiness["previously_ready_downgraded"]:
        pid = item["pathway_id"]
        for b in item["blockers"]:
            d = next(
                (
                    x
                    for x in decisions
                    if x.pathway_id == pid and x.requirement == b["requirement"]
                ),
                None,
            )
            if d is None:
                continue
            actions.append(_action_from_decision(d, impact_boost=-10))

    for d in decisions:
        if d.proposed_classification in {"COMPLETE", "NOT_APPLICABLE"}:
            if d.evidence_quality_issue in {
                "incorrect_match_snippet",
                "cross_pathway_or_contaminated_heading",
            }:
                actions.append(_action_from_decision(d, impact_boost=-5))
            continue
        actions.append(_action_from_decision(d))

    # Deduplicate by pathway+requirement+category
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for a in actions:
        key = (a["pathway_id"], a["requirement"], a["remediation_category"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(a)

    unique.sort(
        key=lambda a: (
            a["priority_score"],
            a["pathway_id"],
            a["requirement"],
        )
    )
    for i, a in enumerate(unique, start=1):
        a["rank"] = i

    return {
        "phase": "6F.0",
        "pending_human_approval_required_for_source_or_allowlist_changes": True,
        "no_fabricated_urls": True,
        "actions": unique,
        "top_10": unique[:10],
        "grounded_url_pool_note": (
            "Only URLs already present on approved ingested documents / prior artifacts "
            "are referenced in action source_url fields."
        ),
        "grounded_urls_sample": (grounded_urls or [])[:30],
    }


def _action_from_decision(
    d: FieldDecision, *, impact_boost: int = 0
) -> dict[str, Any]:
    base = IMPACT_ORDER.get(d.remediation_category, 9)
    # Prefer fixes that restore formerly-ready pathways / matching errors
    score = base + impact_boost
    if d.pathway_id in {
        "gb_skilled_worker",
        "nl_naturalisation",
        "nz_citizenship",
        "ie_employment_permits",
    }:
        score -= 3
    return {
        "pathway_id": d.pathway_id,
        "requirement": d.requirement,
        "proposed_classification": d.proposed_classification,
        "evidence_category": d.evidence_category,
        "remediation_category": d.remediation_category,
        "requires": _requires_bucket(d.remediation_category),
        "source_url": d.source_url,
        "document_id": d.document_id,
        "explanation": d.explanation,
        "priority_score": score,
        "approval_status": (
            "pending_human_approval"
            if d.remediation_category
            in {"propose_allowlist_expansion", "discover_missing_page"}
            else "audit_recommendation_only"
        ),
    }


def _requires_bucket(category: str) -> str:
    return {
        "correct_evidence_selection": "1_correct_evidence_selection_or_classification",
        "reprocess_extraction": "2_reprocess_existing_extraction",
        "inspect_existing_document": "3_inspect_existing_official_document",
        "discover_missing_page": "4_discover_missing_official_page",
        "propose_allowlist_expansion": "5_propose_approved_source_or_allowlist_expansion",
        "human_verification": "6_human_verification",
    }.get(category, category)
