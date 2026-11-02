"""Provisional readiness recalculation from Phase 6F.1 reassessments."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from lifepilot.coverage.evidence_remediation.assessments import FieldAssessment, pathway_ready

CRITICAL_FIELDS = (
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
)

# Original 6F.0 identifiers mapped to 6F.1 normalized ids where renamed.
PREVIOUSLY_PROVISIONAL_READY_6F0 = ("gb_skilled_worker",)
PREVIOUSLY_READY_BEFORE_6F0 = (
    "gb_skilled_worker",
    "nl_naturalisation",
    "nz_citizenship",
    "ie_employment_permits",
)
TARGET_PATHWAYS_6F1 = (
    "gb_skilled_worker",
    "nl_naturalisation",
    "nz_citizenship_by_grant",
    "ie_employment_permits_hub",
    "ie_critical_skills_employment_permit",
    "ie_general_employment_permit",
    "sg_employment_pass",
    "sg_permanent_resident",
    "ca_express_entry",
    "nl_highly_skilled_migrant",
    "nl_permanent_residence",
    "nl_student_residence",
    "au_student_500",
)


def group_by_pathway(
    assessments: list[FieldAssessment],
) -> dict[str, list[FieldAssessment]]:
    out: dict[str, list[FieldAssessment]] = defaultdict(list)
    for a in assessments:
        out[a.pathway_id].append(a)
    return dict(out)


def blockers_for(cells: list[FieldAssessment]) -> list[dict[str, Any]]:
    return [
        {
            "requirement": a.requirement,
            "classification": a.classification,
            "previous_6f0": a.previous_6f0,
            "remediation": a.remediation_remaining,
            "evidence_repair": a.evidence_repair,
        }
        for a in cells
        if a.entity_type == "pathway"
        and a.classification not in {"COMPLETE", "NOT_APPLICABLE"}
    ]


def recalculate_readiness(assessments: list[FieldAssessment]) -> dict[str, Any]:
    by_pathway = group_by_pathway(assessments)

    provisional_ready: list[str] = []
    hub_entities: list[str] = []
    pathway_blockers: list[dict[str, Any]] = []

    for pid, cells in sorted(by_pathway.items()):
        if any(c.entity_type == "information_hub" for c in cells):
            hub_entities.append(pid)
            continue
        pathway_cells = [c for c in cells if c.entity_type == "pathway"]
        # Ensure all critical fields present for readiness.
        by_req = {c.requirement: c for c in pathway_cells}
        missing_fields = [f for f in CRITICAL_FIELDS if f not in by_req]
        ready = pathway_ready(pathway_cells) and not missing_fields
        if ready:
            provisional_ready.append(pid)
        else:
            pathway_blockers.append(
                {
                    "pathway_id": pid,
                    "missing_critical_fields": missing_fields,
                    "blockers": blockers_for(pathway_cells),
                }
            )

    retained = [p for p in PREVIOUSLY_PROVISIONAL_READY_6F0 if p in provisional_ready]
    newly_ready = [p for p in provisional_ready if p not in PREVIOUSLY_PROVISIONAL_READY_6F0]

    # Original pre-6F.0 "ready" set after normalization
    downgraded_legacy = []
    legacy_map = {
        "nl_naturalisation": "nl_naturalisation",
        "nz_citizenship": "nz_citizenship_by_grant",
        "ie_employment_permits": "ie_employment_permits_hub",
    }
    for original, normalized in legacy_map.items():
        cells = by_pathway.get(normalized, [])
        if normalized in hub_entities:
            downgraded_legacy.append(
                {
                    "original_pathway_id": original,
                    "normalized_id": normalized,
                    "status": "information_hub_not_ready",
                    "blockers": [],
                }
            )
            continue
        if normalized not in provisional_ready:
            downgraded_legacy.append(
                {
                    "original_pathway_id": original,
                    "normalized_id": normalized,
                    "status": "not_provisionally_ready",
                    "blockers": blockers_for(cells),
                }
            )

    complete_count = sum(1 for a in assessments if a.classification == "COMPLETE")
    partial_count = sum(1 for a in assessments if a.classification == "PARTIAL")
    missing_count = sum(1 for a in assessments if a.classification == "MISSING")
    uneval_count = sum(1 for a in assessments if a.classification == "UNEVALUATED")
    na_count = sum(1 for a in assessments if a.classification == "NOT_APPLICABLE")

    false_positive_removed = [
        {
            "pathway_id": a.pathway_id,
            "requirement": a.requirement,
            "from": a.previous_6f0,
            "to": a.classification,
        }
        for a in assessments
        if a.previous_6f0 == "COMPLETE"
        and a.classification not in {"COMPLETE", "NOT_APPLICABLE"}
    ]
    promoted = [
        {
            "pathway_id": a.pathway_id,
            "requirement": a.requirement,
            "from": a.previous_6f0,
            "to": a.classification,
            "document_id": a.document_id,
            "evidence_repair": a.evidence_repair,
        }
        for a in assessments
        if a.classification == "COMPLETE"
        and a.previous_6f0 in {"PARTIAL", "MISSING", "UNEVALUATED"}
    ]

    return {
        "readiness_is_automated_provisional_only": True,
        "human_verified_ready_pathways": [],
        "provisional_ready_before_6f1": list(PREVIOUSLY_PROVISIONAL_READY_6F0),
        "provisional_ready_after_6f1": provisional_ready,
        "previously_ready_retained": retained,
        "newly_provisionally_ready": newly_ready,
        "downgraded_or_still_blocked_from_pre_6f0_ready_set": downgraded_legacy,
        "information_hubs_assessed": hub_entities,
        "pathway_blockers": pathway_blockers,
        "classification_counts": {
            "COMPLETE": complete_count,
            "PARTIAL": partial_count,
            "MISSING": missing_count,
            "UNEVALUATED": uneval_count,
            "NOT_APPLICABLE": na_count,
            "fields_assessed": len(assessments),
        },
        "false_positive_complete_removed": false_positive_removed,
        "fields_promoted_to_complete": promoted,
        "targeted_pathways": list(TARGET_PATHWAYS_6F1),
        "note": (
            "No human_verified status assigned. Accuracy preferred over readiness count."
        ),
    }
