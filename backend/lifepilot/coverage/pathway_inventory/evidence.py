"""Evidence coverage cells for Phase 6G pathways."""

from __future__ import annotations

from typing import Any

from lifepilot.coverage.source_gap_closure.assessments import build_assessments as build_6f2
from lifepilot.coverage.pathway_inventory.inventory import build_inventory_rows

CRITICAL = (
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
)

# Map 6F2 pathway ids → 6G inventory ids where renamed
ALIAS = {
    "ca_express_entry": "ca_express_entry_hub",
    "ie_employment_permits_hub": "ie_employment_permits_hub",
    "nz_citizenship_by_grant": "nz_citizenship_by_grant",
}


def _seed_6f2_map(*, ica_doc_id: int | None = 2252) -> dict[tuple[str, str], Any]:
    out: dict[tuple[str, str], Any] = {}
    for a in build_6f2(ica_doc_id=ica_doc_id):
        pid = ALIAS.get(a.pathway_id, a.pathway_id)
        out[(pid, a.requirement)] = a
    # Do not auto-copy hub eligibility COMPLETE onto CA child programs
    return out


def build_evidence_rows(
    *,
    ica_doc_id: int | None = 2252,
    inventory_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    inventory = inventory_rows if inventory_rows is not None else build_inventory_rows()
    prior = _seed_6f2_map(ica_doc_id=ica_doc_id)
    rows: list[dict[str, Any]] = []

    for item in inventory:
        pid = item["pathway_id"]
        entity = item["entity_type"]
        for req in CRITICAL:
            key = (pid, req)
            prior_a = prior.get(key)
            if entity == "information_hub" and req != "official_provenance":
                classification = "NOT_APPLICABLE"
                explanation = "Information hub; assess child true pathways for this requirement."
                passage = ""
                doc_id = ""
                version_id = ""
                url = item.get("official_url") or ""
                repair = "hub_na"
            elif entity == "cross_cutting_guidance" and req not in {
                "official_provenance",
                "financial_or_sponsorship",
            }:
                classification = "UNEVALUATED"
                explanation = "Cross-cutting guidance; pathway-specific completeness not claimed."
                passage = ""
                doc_id = ""
                version_id = ""
                url = item.get("official_url") or ""
                repair = "cross_cutting"
            elif entity == "unknown_candidate":
                classification = "UNEVALUATED"
                explanation = "Pathway scope or official source not yet established."
                passage = ""
                doc_id = ""
                version_id = ""
                url = item.get("official_url") or ""
                repair = "unknown"
            elif prior_a is not None:
                # Special case: CA hub financial was COMPLETE on old ca_express_entry —
                # keep on cross-cutting / do not assign to children automatically.
                classification = prior_a.classification
                explanation = prior_a.explanation
                passage = prior_a.passage
                doc_id = prior_a.document_id or ""
                version_id = prior_a.version_id or ""
                url = prior_a.source_url
                repair = prior_a.evidence_repair
            elif pid in {
                "ca_federal_skilled_worker",
                "ca_canadian_experience_class",
                "ca_federal_skilled_trades",
            }:
                # New program entities: inherit provenance only; leave eligibility/docs open
                if req == "official_provenance":
                    classification = "COMPLETE" if item["approved_source_present"] else "PARTIAL"
                    explanation = "IRCC Express Entry approved source 9083 covers program pages."
                    passage = item.get("official_url") or ""
                    doc_id = ""
                    version_id = ""
                    url = passage
                    repair = "normalized_split"
                elif req == "financial_or_sponsorship" and pid == "ca_canadian_experience_class":
                    classification = "NOT_APPLICABLE"
                    explanation = (
                        "CEC applicants generally exempt from proof of funds when invited "
                        "under CEC (see IRCC proof-of-funds page); confirm per invitation."
                    )
                    passage = (
                        "You don’t need proof of funds if you’re applying under the "
                        "Canadian Experience Class (conditional exemptions also exist)."
                    )
                    doc_id = 2142
                    version_id = ""
                    url = "https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry/documents/proof-funds.html"
                    repair = "normalized_split"
                elif req == "financial_or_sponsorship":
                    classification = "PARTIAL"
                    explanation = (
                        "Settlement funds table exists on shared proof-of-funds page; "
                        "program-specific applicability must be confirmed (not inherited as COMPLETE)."
                    )
                    passage = "Proof of funds required for FSW/FST; amounts in CAD table on IRCC page."
                    doc_id = 2142
                    version_id = ""
                    url = "https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry/documents/proof-funds.html"
                    repair = "normalized_split"
                else:
                    classification = "UNEVALUATED"
                    explanation = (
                        "New program entity after Express Entry split; do not inherit hub COMPLETE."
                    )
                    passage = ""
                    doc_id = ""
                    version_id = ""
                    url = item.get("official_url") or ""
                    repair = "normalized_split"
            elif req == "official_provenance":
                if item["approved_source_present"]:
                    classification = "COMPLETE"
                    explanation = "Linked approved source present."
                elif item.get("official_url"):
                    classification = "PARTIAL"
                    explanation = "Official URL identified but source not approved/ingested."
                else:
                    classification = "MISSING"
                    explanation = "No official URL or approved source identified."
                passage = item.get("official_url") or ""
                doc_id = ""
                version_id = ""
                url = passage
                repair = "inventory"
            else:
                if item["ingested_documents"] > 0:
                    classification = "UNEVALUATED"
                    explanation = "Documents ingested but this critical field not yet deep-validated in 6G."
                else:
                    classification = "MISSING" if item.get("official_url") else "UNEVALUATED"
                    explanation = (
                        "No ingested pathway evidence yet."
                        if classification == "MISSING"
                        else "Not yet researched / no URL."
                    )
                passage = ""
                doc_id = ""
                version_id = ""
                url = item.get("official_url") or ""
                repair = "coverage_gap"

            rows.append(
                {
                    "pathway_id": pid,
                    "country": item["country"],
                    "category": item["category"],
                    "entity_type": entity,
                    "requirement": req,
                    "classification": classification,
                    "document_id": doc_id,
                    "version_id": version_id,
                    "source_url": url,
                    "passage": (passage or "")[:500],
                    "explanation": explanation,
                    "evidence_repair": repair,
                    "reviewer_status": "automated_provisional",
                    "ingested_documents": item["ingested_documents"],
                    "approved_source_present": item["approved_source_present"],
                }
            )
    return rows


def evidence_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    true_cells = [r for r in rows if r["entity_type"] == "true_pathway"]
    applicable = [r for r in true_cells if r["classification"] != "NOT_APPLICABLE"]
    complete = [r for r in applicable if r["classification"] == "COMPLETE"]
    by_pathway: dict[str, list[dict[str, Any]]] = {}
    for r in true_cells:
        by_pathway.setdefault(r["pathway_id"], []).append(r)

    provisionally_ready = []
    for pid, cells in by_pathway.items():
        app = [c for c in cells if c["classification"] != "NOT_APPLICABLE"]
        if app and all(c["classification"] == "COMPLETE" for c in app):
            provisionally_ready.append(pid)

    return {
        "true_pathway_critical_cells": len(true_cells),
        "applicable_cells": len(applicable),
        "complete_applicable_cells": len(complete),
        "complete_rate_applicable": (
            round(len(complete) / len(applicable), 4) if applicable else 0.0
        ),
        "denominator_definition": (
            "applicable critical cells on true_pathway entities only "
            "(excludes hubs, unknown_candidate, NOT_APPLICABLE)"
        ),
        "provisionally_ready_pathways_from_matrix": sorted(provisionally_ready),
    }
