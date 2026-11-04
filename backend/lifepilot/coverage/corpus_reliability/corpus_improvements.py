"""Targeted improvements from already-ingested pathway-specific documents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifepilot.coverage.corpus_reliability.assessment import get_classification
from lifepilot.coverage.corpus_reliability.pathway_applicability import document_applies_to_pathway

# Owner-approved checklist PDFs ingested in Phase 6H (required_documents only).
IE_CHECKLIST_DOCS = {
    "ie_critical_skills_employment_permit": {
        "document_id": 2262,
        "url": "https://enterprise.gov.ie/en/publications/publication-files/critical-skills-employment-permits-checklist.pdf",
    },
    "ie_general_employment_permit": {
        "document_id": 2263,
        "url": "https://enterprise.gov.ie/en/publications/publication-files/general-employment-permits-checklist.pdf",
    },
}

SG_PROVENANCE_DOC_ID = 1538


def _load_extraction(session: Session, data_dir: Path, document_id: int) -> tuple[int | None, str, list[dict[str, Any]]]:
    row = session.execute(
        text(
            """
            SELECT dv.id AS version_id, d.canonical_url, de.storage_path
            FROM documents d
            JOIN document_versions dv ON dv.document_id = d.id AND dv.is_current = true
            JOIN document_extractions de ON de.document_version_id = dv.id AND de.status = 'success'
            WHERE d.id = :doc_id
            LIMIT 1
            """
        ),
        {"doc_id": document_id},
    ).mappings().first()
    if not row:
        return None, "", []
    path = data_dir / row["storage_path"]
    if not path.is_file():
        alt = Path(row["storage_path"])
        path = alt if alt.is_file() else path
    if not path.is_file():
        return int(row["version_id"]), row["canonical_url"], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        blocks = payload.get("blocks") or payload.get("content", {}).get("blocks", [])
    except (OSError, json.JSONDecodeError):
        blocks = []
    return int(row["version_id"]), row["canonical_url"], blocks


def _passage_from_blocks(blocks: list[dict[str, Any]], limit: int = 400) -> str:
    parts: list[str] = []
    for b in blocks:
        t = b.get("type")
        if t in ("heading", "paragraph", "list_item"):
            text = (b.get("text") or "").strip()
            if text:
                parts.append(text)
        elif t == "list":
            parts.extend(str(i) for i in (b.get("items") or [])[:12])
        if sum(len(p) for p in parts) > limit:
            break
    return " ".join(parts)[:limit]


def improve_from_existing_corpus(
    session: Session,
    evidence_rows: list[dict[str, Any]],
    data_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Improve high-priority cells using pathway-applicable stored documents only."""
    rows = [dict(r) for r in evidence_rows]
    index = {(r["pathway_id"], r["requirement"]): r for r in rows}
    changes: list[dict[str, Any]] = []

    # IE: checklist PDF may COMPLETE required_documents only (not eligibility/process).
    for pid, meta in IE_CHECKLIST_DOCS.items():
        key = (pid, "required_documents")
        row = index.get(key)
        if not row or row.get("entity_type") != "true_pathway":
            continue
        if get_classification(row) not in ("PARTIAL", "UNEVALUATED", "MISSING"):
            continue
        appl = document_applies_to_pathway(pathway_id=pid, source_url=meta["url"])
        if not appl.applies:
            continue
        ver_id, url, blocks = _load_extraction(session, data_dir, meta["document_id"])
        if ver_id is None:
            continue
        passage = _passage_from_blocks(blocks) or (
            f"Official {pid} document checklist PDF (required_documents only; "
            "eligibility/process remain on permit guidance pages)."
        )
        prev = get_classification(row)
        row["classification"] = "COMPLETE"
        row["document_id"] = meta["document_id"]
        row["version_id"] = ver_id
        row["source_url"] = url or meta["url"]
        row["passage"] = passage
        row["evidence_basis"] = "official_checklist_pdf"
        row["evidence_repair"] = "phase_6j_ie_checklist_required_documents_only"
        row["explanation"] = (
            "Official permit checklist PDF supports required_documents only; "
            "do not treat as eligibility or application_process evidence."
        )
        row["method"] = "pathway_aware_corpus_improvement"
        changes.append({
            "pathway_id": pid,
            "requirement": "required_documents",
            "prev_classification": prev,
            "new_classification": "COMPLETE",
            "document_id": meta["document_id"],
            "version_id": ver_id,
            "source_url": row["source_url"],
            "passage": passage[:200],
            "rationale": row["explanation"],
            "method": "pathway_aware_corpus_improvement",
            "action": "ie_checklist_required_documents",
        })

    # SG: deepen official_provenance passage from the MOM Employment Pass hub page.
    sg_key = ("sg_employment_pass", "official_provenance")
    sg_row = index.get(sg_key)
    if sg_row and get_classification(sg_row) == "COMPLETE":
        ver_id, url, blocks = _load_extraction(session, data_dir, SG_PROVENANCE_DOC_ID)
        passage = _passage_from_blocks(blocks, limit=500)
        if passage and len(passage) >= 50:
            prev_passage = sg_row.get("passage") or ""
            if len(passage) > len(prev_passage):
                sg_row["passage"] = passage
                sg_row["document_id"] = SG_PROVENANCE_DOC_ID
                if ver_id:
                    sg_row["version_id"] = ver_id
                sg_row["source_url"] = url or sg_row.get("source_url")
                sg_row["evidence_repair"] = "phase_6j_sg_provenance_passage_refresh"
                sg_row["method"] = "pathway_aware_corpus_improvement"
                changes.append({
                    "pathway_id": "sg_employment_pass",
                    "requirement": "official_provenance",
                    "prev_classification": "COMPLETE",
                    "new_classification": "COMPLETE",
                    "document_id": SG_PROVENANCE_DOC_ID,
                    "version_id": ver_id or "",
                    "source_url": sg_row.get("source_url") or "",
                    "passage": passage[:200],
                    "rationale": "Refreshed provenance passage from MOM Employment Pass page; still not human_verified.",
                    "method": "pathway_aware_corpus_improvement",
                    "action": "sg_provenance_passage_refresh",
                })

    return rows, changes
