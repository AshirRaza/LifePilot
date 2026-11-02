"""Automated, provisional readiness for Phase 6H (never human-verified)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from lifepilot.coverage.source_expansion.evidence import has_evidence

PREVIOUSLY_READY_6G = (
    "gb_skilled_worker",
    "nl_highly_skilled_migrant",
    "nl_naturalisation",
    "sg_employment_pass",
)
FOCUS_PATHWAYS = (
    "ca_federal_skilled_worker",
    "ca_canadian_experience_class",
    "ca_federal_skilled_trades",
    "ie_critical_skills_employment_permit",
    "ie_general_employment_permit",
    "nl_permanent_residence",
    "gb_student",
    "de_eu_blue_card",
    "au_temporary_graduate_485",
    "us_f1",
    "ae_work_residence",
    "jp_engineer_specialist",
    "fr_passeport_talent",
    "nz_citizenship_by_grant",
)


def assert_no_human_verified(rows: list[dict[str, Any]]) -> None:
    bad = [r for r in rows if "human" in str(r.get("reviewer_status", "")).lower()]
    if bad:
        raise RuntimeError(f"human_verified status is not permitted in 6H: {len(bad)} rows")


def build_readiness_6h(
    evidence_rows: list[dict[str, Any]],
    *,
    evidence_summary: dict[str, Any],
    change_summary: dict[str, Any],
    entity_reconciliation: dict[str, Any],
    metrics_totals: dict[str, Any],
    ingestion: dict[str, Any],
    canada_summary: dict[str, Any],
    storage: dict[str, Any],
    baseline: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    assert_no_human_verified(evidence_rows)
    ready = list(evidence_summary["provisionally_ready_pathways_from_matrix"])
    retained = [p for p in PREVIOUSLY_READY_6G if p in ready]
    newly = [p for p in ready if p not in PREVIOUSLY_READY_6G]
    lost = [p for p in PREVIOUSLY_READY_6G if p not in ready]

    true_cells = [r for r in evidence_rows if r["entity_type"] == "true_pathway"]
    blockers = [
        {
            "pathway_id": r["pathway_id"],
            "country": r["country"],
            "requirement": r["requirement"],
            "classification": r["classification"],
            "reason": str(r.get("explanation") or "")[:200],
        }
        for r in true_cells
        if (r["pathway_id"] in set(FOCUS_PATHWAYS) | set(PREVIOUSLY_READY_6G))
        and r["classification"] not in {"COMPLETE", "NOT_APPLICABLE"}
    ]
    by_cls: dict[str, int] = defaultdict(int)
    for r in true_cells:
        by_cls[r["classification"]] += 1

    complete_missing_evidence = [
        r["pathway_id"] + "/" + r["requirement"]
        for r in true_cells
        if r["classification"] == "COMPLETE" and not has_evidence(r)
    ]
    declined = [d for d in decisions if str(d.get("decision")) != "approved"]
    return {
        "phase": "6H",
        "readiness_is_automated_provisional_only": True,
        "human_verified_ready_pathways": [],
        "provisionally_ready_pathways": ready,
        "previously_ready_6g": list(PREVIOUSLY_READY_6G),
        "retained_from_6g": retained,
        "newly_provisionally_ready": newly,
        "lost_from_6g": lost,
        "evidence_summary": evidence_summary,
        "evidence_change_summary_true_pathway_cells": change_summary,
        "entity_reconciliation": {
            k: entity_reconciliation[k]
            for k in (
                "total_entities", "entity_type_counts", "entity_type_sum", "reconciles",
                "matches_expected_149", "formula",
            )
        },
        "metrics_totals": metrics_totals,
        "canada": canada_summary,
        "storage_integrity_ok": storage.get("integrity_ok"),
        "ingestion_delta": ingestion.get("delta"),
        "source_decisions": {
            "approved": [d["candidate_id"] for d in decisions if d.get("decision") == "approved"],
            "rejected": [
                {"candidate_id": d["candidate_id"], "code": d.get("decision_code")} for d in declined
            ],
        },
        "baseline_ready_6g": baseline.get("expected_from_6g", {}).get("provisionally_ready"),
        "true_pathway_classification_counts": dict(by_cls),
        "complete_cells_missing_evidence": complete_missing_evidence,
        "top_blockers": blockers[:60],
        "invariants": {
            "no_human_verified_status": True,
            "complete_requires_evidence": not complete_missing_evidence,
            "baseline_preserved": True,
        },
        "note": (
            "Provisional readiness means every applicable critical cell is COMPLETE with stored "
            "evidence under automated rules. It is not human verification and not national "
            "coverage."
        ),
    }
