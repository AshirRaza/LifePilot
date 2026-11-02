"""Phase 6H evidence overlay on the Phase 6G matrix.

Ordering of overlays (each step records why a cell changed):

1. Phase 6G base cell.
2. Canada FSW/CEC/FST: independent deterministic assessment (replaces 6G cells).
3. Pathway-specific auto-match from newly ingested/extracted pages: at most PARTIAL
   ("proposed_not_verified"); never COMPLETE.
4. IE/NL blocker upgrades: an itemised official checklist PDF newly ingested under an
   owner-approved exact URL may complete ``required_documents``; a form-only PDF yields PARTIAL.
5. Provenance: link via approved source + pathway-specific document -> COMPLETE; hub-only
   inference from a source link is downgraded to PARTIAL.
6. BLOCKED when an owner-approved fetch was blocked/failed and the cell has no other evidence.
"""

from __future__ import annotations

import json
from typing import Any

from lifepilot.coverage.source_expansion.relevance import (
    ExtractionRecord,
    classify_pdf_kind,
    find_requirement_sections,
)

CRITICAL = (
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
)
CONTENT_REQS = ("eligibility", "required_documents", "application_process", "financial_or_sponsorship")
CA_PROGRAMS = (
    "ca_federal_skilled_worker",
    "ca_canadian_experience_class",
    "ca_federal_skilled_trades",
)
RANK = {
    "BLOCKED": 0,
    "UNEVALUATED": 1,
    "MISSING": 2,
    "PARTIAL": 3,
    "NOT_APPLICABLE": 3,
    "COMPLETE": 4,
}
LOW = {"UNEVALUATED", "MISSING"}
CHECKLIST_KINDS = {"document_checklist", "generic_checklist"}
FORM_CUE_LIMIT = 4  # >= this many form-field cues => application form, not a checklist


def has_evidence(row: dict[str, Any]) -> bool:
    """A COMPLETE cell must cite a stored document, or (provenance) an approved source."""
    if row.get("document_id") not in ("", None):
        return True
    if row["requirement"] == "official_provenance" and row.get("evidence_source_ids"):
        return True
    return False


def complete_without_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r["classification"] == "COMPLETE" and not has_evidence(r)]


def change_type(before: str, after: str) -> str:
    if before == after:
        return "unchanged"
    if before == "UNEVALUATED" and after != "UNEVALUATED":
        return "newly_assessed"
    if RANK[after] > RANK[before]:
        return "promoted"
    if RANK[after] < RANK[before]:
        return "downgraded"
    return "reclassified"


def _first_extracted(
    doc_ids: list[int], extractions: dict[int, ExtractionRecord]
) -> list[ExtractionRecord]:
    return [extractions[d] for d in doc_ids if d in extractions]


def build_evidence_6h(
    *,
    base_rows: list[dict[str, Any]],
    pathway_metrics: list[dict[str, Any]],
    extractions: dict[int, ExtractionRecord],
    current_version_by_doc: dict[int, int],
    canada_rows: list[dict[str, Any]],
    new_doc_ids: set[int],
    blocked_pathways: dict[str, str],
    assessed_at: str,
    published_6g: dict[tuple[str, str], str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pm_by_id = {p["pathway_id"]: p for p in pathway_metrics}
    canada_by_key = {(r["pathway_id"], r["requirement"]): r for r in canada_rows}
    rows: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []

    for base in base_rows:
        pid = base["pathway_id"]
        req = base["requirement"]
        entity = base["entity_type"]
        row = dict(base)
        row["classification_6g"] = (published_6g or {}).get((pid, req), base["classification"])
        row["evidence_basis"] = "phase_6g_base"
        row["evidence_span"] = ""
        row["evidence_source_ids"] = []
        row["shared_evidence"] = False
        row["assessed_at_6h"] = ""
        row["version_id_resolved_from_current"] = False
        pm = pm_by_id.get(pid, {})

        if (pid, req) in canada_by_key:
            c = canada_by_key[(pid, req)]
            row.update(
                classification=c["classification"],
                document_id=c["document_id"],
                version_id=c["version_id"],
                source_url=c["source_url"],
                passage=c["passage"][:500],
                explanation=c["rationale"],
                evidence_repair="phase_6h_canada_independent",
                evidence_basis="canada_deterministic_blocks",
                evidence_span=c["evidence_spans"],
                shared_evidence=bool(c["shared_evidence"]),
                missing_subrequirements=c["missing_subrequirements"],
            )
            if req == "official_provenance" and c["classification"] == "COMPLETE":
                row["evidence_source_ids"] = [9083]
        elif entity == "true_pathway":
            _overlay_true_pathway(
                row, pm, extractions=extractions, new_doc_ids=new_doc_ids,
                blocked_pathways=blocked_pathways,
            )

        # Resolve blank version ids from the document's current version (documented).
        if (
            row["classification"] == "COMPLETE"
            and row.get("document_id") not in ("", None)
            and row.get("version_id") in ("", None)
        ):
            ver = current_version_by_doc.get(int(row["document_id"]))
            if ver:
                row["version_id"] = ver
                row["version_id_resolved_from_current"] = True

        # COMPLETE needs evidence: refuse silently-inherited completes.
        if row["classification"] == "COMPLETE" and not has_evidence(row):
            row["classification"] = "PARTIAL"
            row["explanation"] = (
                "Downgraded in 6H: COMPLETE without stored-document evidence. "
                + str(row.get("explanation") or "")
            )
            row["evidence_repair"] = "phase_6h_complete_without_evidence_downgrade"

        row["assessed_at_6h"] = assessed_at
        rows.append(row)
        changes.append(
            {
                "pathway_id": pid,
                "country": row["country"],
                "entity_type": entity,
                "requirement": req,
                "classification_6g": row["classification_6g"],
                "classification_6h": row["classification"],
                "change": change_type(row["classification_6g"], row["classification"]),
                "evidence_repair": row.get("evidence_repair", ""),
                "evidence_basis": row.get("evidence_basis", ""),
                "document_id": row.get("document_id", ""),
                "version_id": row.get("version_id", ""),
                "reason": str(row.get("explanation") or "")[:300],
            }
        )
    return rows, changes


def _overlay_true_pathway(
    row: dict[str, Any],
    pm: dict[str, Any],
    *,
    extractions: dict[int, ExtractionRecord],
    new_doc_ids: set[int],
    blocked_pathways: dict[str, str],
) -> None:
    pid = row["pathway_id"]
    req = row["requirement"]
    strong = list(pm.get("pathway_strong_document_ids") or [])
    shared = list(pm.get("pathway_shared_document_ids") or [])
    linked = bool(pm.get("pathway_source_linked"))
    linked_ids = list(pm.get("linked_source_ids") or [])
    cls = row["classification"]

    if req == "official_provenance":
        if row["evidence_repair"] == "inventory" or cls in LOW | {"PARTIAL"}:
            if linked and (strong or shared):
                if cls != "COMPLETE":
                    row.update(
                        classification="COMPLETE",
                        explanation=(
                            "Approved source linked to this pathway and a pathway-specific "
                            f"page is stored (documents {sorted(set(strong + shared))[:5]})."
                        ),
                        evidence_repair="phase_6h_provenance_linked",
                        evidence_basis="approved_source_plus_pathway_document",
                        document_id=(strong or shared)[0],
                        passage="",
                    )
                row["evidence_source_ids"] = linked_ids
            elif linked and cls == "COMPLETE":
                row.update(
                    classification="PARTIAL",
                    explanation=(
                        "Downgraded: approved source is linked but no stored page matches this "
                        "pathway's own URL scope (hub-level link only; a matching official host "
                        "is not pathway verification)."
                    ),
                    evidence_repair="phase_6h_hub_only_provenance_downgrade",
                    evidence_basis="hub_only_source_link",
                )
                row["evidence_source_ids"] = linked_ids
            elif linked:
                row.update(
                    classification="PARTIAL",
                    explanation="Approved source linked but no pathway-specific page stored.",
                    evidence_repair="phase_6h_provenance_hub_only",
                    evidence_basis="hub_only_source_link",
                )
                row["evidence_source_ids"] = linked_ids
        elif cls == "COMPLETE":
            row["evidence_source_ids"] = linked_ids
        return

    # content requirements
    if cls in LOW and strong:
        recs = _first_extracted(strong, extractions)
        for rec in recs:
            secs = find_requirement_sections(rec.blocks, req)
            if secs:
                s = secs[0]
                row.update(
                    classification="PARTIAL",
                    document_id=rec.document_id,
                    version_id=rec.version_id,
                    source_url=rec.source_url,
                    passage=s.text[:500],
                    explanation=(
                        f"Heading '{s.heading}' ({s.words} words) in a pathway-specific page "
                        "matches this requirement; proposed_not_verified (auto-match is never "
                        "COMPLETE)."
                    ),
                    evidence_repair="phase_6h_auto_match_partial",
                    evidence_basis="pathway_url_scope+heading_section",
                    evidence_span=s.span,
                )
                cls = "PARTIAL"
                break

    if req == "required_documents":
        for rec in _first_extracted(strong, extractions):
            if rec.document_id not in new_doc_ids or rec.content_kind != "pdf":
                continue
            pdf = classify_pdf_kind(rec.blocks, url=rec.source_url, title=rec.title)
            standalone = pdf["form_cue_hits"] < FORM_CUE_LIMIT
            if pdf["lists_documents"] and pdf["kind"] in CHECKLIST_KINDS and standalone:
                row.update(
                    classification="COMPLETE",
                    document_id=rec.document_id,
                    version_id=rec.version_id,
                    source_url=rec.source_url,
                    passage=f"Itemised official checklist PDF ({pdf['kind']}); {pdf['list_span']}.",
                    explanation=(
                        "Owner-approved exact official checklist PDF lists required documents "
                        f"(kind={pdf['kind']}, {pdf['item_rows']} list/table rows)."
                    ),
                    evidence_repair="phase_6h_pdf_checklist_upgrade",
                    evidence_basis="official_checklist_pdf",
                    evidence_span=pdf["list_span"],
                )
                return
            if cls in LOW:
                row.update(
                    classification="PARTIAL",
                    document_id=rec.document_id,
                    version_id=rec.version_id,
                    source_url=rec.source_url,
                    passage=(
                        f"PDF kind={pdf['kind']}; lists_documents={pdf['lists_documents']}; "
                        f"form_cue_hits={pdf['form_cue_hits']}."
                    ),
                    explanation=(
                        "Official PDF ingested but it is an application form/instructions: "
                        "any supporting-document lists are conditional on the application reason "
                        "and may cover several routes, so it is not a standalone checklist for "
                        "this pathway."
                    ),
                    evidence_repair="phase_6h_pdf_not_a_document_list",
                    evidence_basis="official_pdf_non_checklist",
                )
                cls = "PARTIAL"

    if row["classification"] in LOW and pid in blocked_pathways:
        row.update(
            classification="BLOCKED",
            explanation=f"Owner-approved fetch did not produce evidence: {blocked_pathways[pid]}",
            evidence_repair="phase_6h_blocked",
            evidence_basis="fetch_blocked_or_failed",
        )


def evidence_summary_6h(rows: list[dict[str, Any]]) -> dict[str, Any]:
    true_cells = [r for r in rows if r["entity_type"] == "true_pathway"]
    applicable = [r for r in true_cells if r["classification"] != "NOT_APPLICABLE"]
    by_cls: dict[str, int] = {}
    for r in applicable:
        by_cls[r["classification"]] = by_cls.get(r["classification"], 0) + 1
    by_pathway: dict[str, list[dict[str, Any]]] = {}
    for r in true_cells:
        by_pathway.setdefault(r["pathway_id"], []).append(r)
    ready = []
    for pid, cells in by_pathway.items():
        app = [c for c in cells if c["classification"] != "NOT_APPLICABLE"]
        if app and all(c["classification"] == "COMPLETE" and has_evidence(c) for c in app):
            ready.append(pid)
    complete = by_cls.get("COMPLETE", 0)
    return {
        "true_pathway_critical_cells": len(true_cells),
        "applicable_cells": len(applicable),
        "complete_applicable_cells": complete,
        "complete_rate_applicable": round(complete / len(applicable), 4) if applicable else 0.0,
        "classification_counts_applicable": by_cls,
        "unevaluated": by_cls.get("UNEVALUATED", 0),
        "denominator_definition": (
            "applicable critical cells on true_pathway entities only "
            "(excludes hubs, cross_cutting_guidance, unknown_candidate, NOT_APPLICABLE)"
        ),
        "provisionally_ready_pathways_from_matrix": sorted(ready),
    }


def summarize_changes(changes: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, int] = {}
    for c in changes:
        if c["entity_type"] != "true_pathway":
            continue
        out[c["change"]] = out.get(c["change"], 0) + 1
    return out


def serialize(value: Any) -> Any:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
