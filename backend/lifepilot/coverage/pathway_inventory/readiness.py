"""Readiness summary for Phase 6G."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from lifepilot.coverage.pathway_inventory.evidence import evidence_summary

PREVIOUSLY_READY_6F2 = (
    "gb_skilled_worker",
    "nl_naturalisation",
    "nl_highly_skilled_migrant",
    "sg_employment_pass",
)


def build_readiness(
    evidence_rows: list[dict[str, Any]],
    *,
    inventory_summary: dict[str, Any],
    ingestion: dict[str, Any],
) -> dict[str, Any]:
    es = evidence_summary(evidence_rows)
    ready = list(es["provisionally_ready_pathways_from_matrix"])
    # Prefer retaining known 6F2 ready set when still complete
    retained = [p for p in PREVIOUSLY_READY_6F2 if p in ready]
    newly = [p for p in ready if p not in PREVIOUSLY_READY_6F2]
    lost = [p for p in PREVIOUSLY_READY_6F2 if p not in ready]

    blockers = [
        {
            "pathway_id": r["pathway_id"],
            "country": r["country"],
            "requirement": r["requirement"],
            "classification": r["classification"],
        }
        for r in evidence_rows
        if r["entity_type"] == "true_pathway"
        and r["classification"] not in {"COMPLETE", "NOT_APPLICABLE"}
        and r["pathway_id"]
        in set(PREVIOUSLY_READY_6F2)
        | {
            "sg_permanent_resident",
            "au_student_500",
            "ca_federal_skilled_worker",
            "ca_canadian_experience_class",
            "ca_federal_skilled_trades",
            "ie_critical_skills_employment_permit",
            "ie_general_employment_permit",
            "nl_permanent_residence",
            "nl_student_residence",
            "nz_citizenship_by_grant",
        }
    ]

    by_cls: dict[str, int] = defaultdict(int)
    for r in evidence_rows:
        if r["entity_type"] == "true_pathway":
            by_cls[r["classification"]] += 1

    return {
        "readiness_is_automated_provisional_only": True,
        "human_verified_ready_pathways": [],
        "provisionally_ready_pathways": ready,
        "previously_ready_6f2": list(PREVIOUSLY_READY_6F2),
        "retained_from_6f2": retained,
        "newly_provisionally_ready": newly,
        "lost_from_6f2": lost,
        "evidence_summary": es,
        "inventory_summary": {
            "true_pathways": inventory_summary.get("true_pathways"),
            "true_pathways_with_approved_source": inventory_summary.get(
                "true_pathways_with_approved_source"
            ),
            "true_pathways_with_ingested_docs": inventory_summary.get(
                "true_pathways_with_ingested_docs"
            ),
        },
        "ingestion_delta": ingestion.get("delta"),
        "true_pathway_classification_counts": dict(by_cls),
        "top_blockers": blockers[:40],
        "normalization_notes": [
            "ca_express_entry demoted to ca_express_entry_hub; FSW/CEC/FST are separate true pathways",
            "nz citizenship grant/descent/birth kept distinct",
            "nl_permanent_residence vs nl_eu_long_term_resident distinguished",
            "ie employment permits remain hub + Critical Skills + General",
        ],
        "note": "Do not interpret provisional readiness as national coverage complete.",
    }
