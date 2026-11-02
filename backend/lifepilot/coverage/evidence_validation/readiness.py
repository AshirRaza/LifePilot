"""Provisional readiness recalculation from 6F.0 proposed classifications."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from lifepilot.coverage.evidence_validation.decisions import (
    CRITICAL_FIELDS,
    NEAR_READY_PATHWAYS,
    READY_PATHWAYS,
    FieldDecision,
)
from lifepilot.coverage.normalization.normalize import load_normalization_config

COMPLETE_OK = {"COMPLETE", "NOT_APPLICABLE"}


def _financial_applicable(pathway_id: str) -> bool:
    cfg = load_normalization_config()
    for e in cfg.get("entities", []):
        if e.get("normalized_id") == pathway_id or e.get("original_pathway_id") == pathway_id:
            # default true unless notes / we check inventory - use decision N/A
            break
    # Infer from decisions: if any NOT_APPLICABLE financial, treat as not applicable
    return True


def pathway_provisional_ready(decisions: list[FieldDecision]) -> bool:
    by_req = {d.requirement: d for d in decisions}
    for field in CRITICAL_FIELDS:
        d = by_req.get(field)
        if d is None:
            return False
        if d.proposed_classification == "NOT_APPLICABLE":
            continue
        if d.proposed_classification != "COMPLETE":
            return False
    return True


def recalculate_readiness(decisions: list[FieldDecision]) -> dict[str, Any]:
    by_pathway: dict[str, list[FieldDecision]] = defaultdict(list)
    for d in decisions:
        by_pathway[d.pathway_id].append(d)

    previously_ready = set(READY_PATHWAYS)
    remain_ready: list[str] = []
    downgraded: list[dict[str, Any]] = []
    near_ready_promotable: list[str] = []
    blockers: list[dict[str, Any]] = []

    for pid in list(READY_PATHWAYS) + list(NEAR_READY_PATHWAYS):
        cells = by_pathway.get(pid, [])
        if not cells:
            continue
        ready = pathway_provisional_ready(cells)
        incomplete = [
            {
                "requirement": d.requirement,
                "proposed": d.proposed_classification,
                "previous": d.previous_classification,
                "category": d.evidence_category,
                "issue": d.evidence_quality_issue,
                "remediation": d.remediation_category,
            }
            for d in cells
            if d.proposed_classification not in COMPLETE_OK
        ]
        if pid in previously_ready:
            if ready:
                remain_ready.append(pid)
            else:
                downgraded.append({"pathway_id": pid, "blockers": incomplete})
        else:
            if ready:
                near_ready_promotable.append(pid)
            blockers.extend(
                {"pathway_id": pid, **b} for b in incomplete
            )

    matching_problems = [
        b
        for b in blockers + [x for d in downgraded for x in [{"pathway_id": d["pathway_id"], **bb} for bb in d["blockers"]]]
        if b.get("category") == "A"
        or b.get("issue")
        in {
            "incorrect_match_snippet",
            "cross_pathway_or_contaminated_heading",
            "navigation_heading_only",
            "hub_tile_without_list",
            "unrelated_passport_mvv_snippet",
        }
    ]
    genuine_missing = [b for b in blockers if b.get("category") == "C"]
    extraction_problems = [
        b
        for b in blockers
        if b.get("category") == "D"
        or b.get("remediation") in {"reprocess_extraction"}
        or "extraction" in (b.get("issue") or "")
    ]

    # Also count A/B from downgraded
    all_incomplete = []
    for pid, cells in by_pathway.items():
        for d in cells:
            if d.proposed_classification not in COMPLETE_OK:
                all_incomplete.append(d)

    return {
        "readiness_is_automated_provisional_only": True,
        "human_verified_ready_pathways": [],
        "previously_ready_still_provisional_ready": remain_ready,
        "previously_ready_downgraded": downgraded,
        "near_ready_promotable_after_review": near_ready_promotable,
        "provisional_ready_count": len(remain_ready) + len(near_ready_promotable),
        "remaining_critical_blockers": [
            {
                "pathway_id": d.pathway_id,
                "requirement": d.requirement,
                "proposed": d.proposed_classification,
                "category": d.evidence_category,
                "issue": d.evidence_quality_issue,
                "remediation": d.remediation_category,
            }
            for d in all_incomplete
        ],
        "blockers_poor_evidence_matching": [
            {
                "pathway_id": d.pathway_id,
                "requirement": d.requirement,
                "issue": d.evidence_quality_issue,
            }
            for d in all_incomplete
            if d.evidence_category == "A"
            or "incorrect_match" in d.evidence_quality_issue
            or "contaminated" in d.evidence_quality_issue
            or d.evidence_quality_issue == "navigation_heading_only"
        ],
        "blockers_genuine_missing_documents": [
            {
                "pathway_id": d.pathway_id,
                "requirement": d.requirement,
            }
            for d in all_incomplete
            if d.evidence_category == "C"
        ],
        "blockers_extraction_or_provenance": [
            {
                "pathway_id": d.pathway_id,
                "requirement": d.requirement,
                "issue": d.evidence_quality_issue,
            }
            for d in all_incomplete
            if d.evidence_category == "D"
            or d.remediation_category == "reprocess_extraction"
            or "language_metadata" in d.evidence_quality_issue
        ],
        "denominator_rules_preserved": {
            "unevaluated_blocks_readiness": True,
            "hubs_excluded": True,
            "unknown_slots_separate": True,
            "not_assessed_for_zero_denominator_countries": True,
            "no_national_coverage_inference": True,
        },
    }
