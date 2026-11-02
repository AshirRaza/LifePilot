"""Readiness recalculation for Phase 6F.2."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from lifepilot.coverage.source_gap_closure.assessments import FieldAssessment, pathway_ready

CRITICAL_FIELDS = (
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
)

PREVIOUSLY_PROVISIONAL_READY_6F1 = (
    "gb_skilled_worker",
    "nl_naturalisation",
    "sg_employment_pass",
)


def recalculate_readiness(assessments: list[FieldAssessment]) -> dict[str, Any]:
    by_pathway: dict[str, list[FieldAssessment]] = defaultdict(list)
    for a in assessments:
        by_pathway[a.pathway_id].append(a)

    provisional_ready: list[str] = []
    hubs: list[str] = []
    blockers: list[dict[str, Any]] = []

    for pid, cells in sorted(by_pathway.items()):
        if any(c.entity_type == "information_hub" for c in cells):
            hubs.append(pid)
            continue
        pathway_cells = [c for c in cells if c.entity_type == "pathway"]
        by_req = {c.requirement: c for c in pathway_cells}
        missing = [f for f in CRITICAL_FIELDS if f not in by_req]
        if pathway_ready(pathway_cells) and not missing:
            provisional_ready.append(pid)
        else:
            blockers.append(
                {
                    "pathway_id": pid,
                    "missing_critical_fields": missing,
                    "blockers": [
                        {
                            "requirement": c.requirement,
                            "classification": c.classification,
                            "previous_6f1": c.previous_6f1,
                            "remediation": c.remediation_remaining,
                            "evidence_repair": c.evidence_repair,
                        }
                        for c in pathway_cells
                        if c.classification not in {"COMPLETE", "NOT_APPLICABLE"}
                    ],
                }
            )

    retained = [p for p in PREVIOUSLY_PROVISIONAL_READY_6F1 if p in provisional_ready]
    newly = [p for p in provisional_ready if p not in PREVIOUSLY_PROVISIONAL_READY_6F1]
    downgraded = [
        p for p in PREVIOUSLY_PROVISIONAL_READY_6F1 if p not in provisional_ready
    ]

    promoted = [
        {
            "pathway_id": a.pathway_id,
            "requirement": a.requirement,
            "from": a.previous_6f1,
            "to": a.classification,
            "document_id": a.document_id,
            "evidence_repair": a.evidence_repair,
        }
        for a in assessments
        if a.classification == "COMPLETE"
        and a.previous_6f1 in {"PARTIAL", "MISSING", "UNEVALUATED"}
    ]
    downgraded_fields = [
        {
            "pathway_id": a.pathway_id,
            "requirement": a.requirement,
            "from": a.previous_6f1,
            "to": a.classification,
        }
        for a in assessments
        if a.previous_6f1 == "COMPLETE"
        and a.classification not in {"COMPLETE", "NOT_APPLICABLE"}
    ]

    counts = {
        cls: sum(1 for a in assessments if a.classification == cls)
        for cls in (
            "COMPLETE",
            "PARTIAL",
            "MISSING",
            "UNEVALUATED",
            "NOT_APPLICABLE",
            "BLOCKED",
        )
    }
    counts["fields_assessed"] = len(assessments)

    return {
        "readiness_is_automated_provisional_only": True,
        "human_verified_ready_pathways": [],
        "provisional_ready_before_6f2": list(PREVIOUSLY_PROVISIONAL_READY_6F1),
        "provisional_ready_after_6f2": provisional_ready,
        "previously_ready_retained": retained,
        "newly_provisionally_ready": newly,
        "downgraded_from_6f1_ready": downgraded,
        "information_hubs_assessed": hubs,
        "pathway_blockers": blockers,
        "classification_counts": counts,
        "fields_promoted_to_complete": promoted,
        "fields_downgraded_from_complete": downgraded_fields,
        "pathway_normalization_concerns": [
            {
                "pathway_id": "ca_express_entry",
                "issue": "covers_three_programs_with_distinct_eligibility",
                "recommendation": (
                    "Normalize into hub + FSW / CEC / FST pathways using already-ingested "
                    "who-can-apply child pages."
                ),
            }
        ],
        "note": "Accuracy preferred over readiness count. No human_verified status assigned.",
    }
