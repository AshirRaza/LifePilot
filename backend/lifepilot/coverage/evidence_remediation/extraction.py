"""Narrow extraction remediation for Phase 6F.1 (AU Student 500 PageSchema)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from lifepilot.crawl.storage import data_root
from lifepilot.db.models import Document, DocumentExtraction, DocumentVersion
from lifepilot.db.session import get_session_factory
from lifepilot.extract.html_structured import (
    EXTRACTOR_VERSION,
    extract_html_document,
)
from lifepilot.extract.runner import extract_document_version

AU_STUDENT_DOC_ID = 2218


def verify_au_financial_in_raw_and_extract() -> dict[str, Any]:
    """Confirm financial guidance exists in stored raw HTML and current extract."""
    session = get_session_factory()()
    try:
        version = session.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == AU_STUDENT_DOC_ID,
                DocumentVersion.is_current.is_(True),
            )
        ).scalar_one()
        document = session.get(Document, AU_STUDENT_DOC_ID)
        assert document is not None
        root = data_root()
        raw_path = root / version.storage_path
        raw_bytes = raw_path.read_bytes()
        raw_lower = raw_bytes.decode("utf-8", errors="replace").lower()
        raw_markers = {
            "enough_money": "evidence you have enough money" in raw_lower,
            "gather_documents": "gather documents" in raw_lower,
            "financial_capacity": "financial capacity" in raw_lower,
            "genuine_student": "genuine student" in raw_lower,
        }
        fresh = extract_html_document(raw_bytes)
        fresh_blob = " ".join(
            (b.text or "") + " " + " ".join(b.items or ()) for b in fresh.blocks
        ).lower()
        extract_markers = {
            "enough_money": "enough money" in fresh_blob,
            "gather_documents": "gather documents" in fresh_blob,
            "financial_capacity": "financial capacity" in fresh_blob,
            "block_count": len(fresh.blocks),
            "title": fresh.title,
        }

        existing = session.execute(
            select(DocumentExtraction).where(
                DocumentExtraction.document_version_id == version.id,
                DocumentExtraction.extractor_name == "html_structured",
            )
        ).scalar_one_or_none()

        dry = extract_document_version(
            session, version, document, persist=False, storage_root=root
        )

        # Re-extract with persist; runner keeps historical file when content/version match
        # (status=unchanged) and otherwise upserts a new extraction JSON for this version.
        persisted = extract_document_version(
            session, version, document, persist=True, storage_root=root
        )
        session.commit()
        persist_result = {
            "status": persisted.status,
            "message": persisted.message,
            "extraction_id": persisted.extraction_id,
            "storage_path": persisted.storage_path,
            "block_count": persisted.block_count,
        }

        improved = bool(
            raw_markers.get("enough_money") and extract_markers.get("enough_money")
        )
        return {
            "document_id": AU_STUDENT_DOC_ID,
            "version_id": version.id,
            "storage_path": version.storage_path,
            "extractor_version": EXTRACTOR_VERSION,
            "raw_markers": raw_markers,
            "fresh_extract_markers": extract_markers,
            "existing_extraction": {
                "id": getattr(existing, "id", None),
                "extractor_version": getattr(existing, "extractor_version", None),
                "status": getattr(existing, "status", None),
                "storage_path": getattr(existing, "storage_path", None),
            },
            "dry_run": {
                "status": dry.status,
                "block_count": dry.block_count,
                "message": dry.message,
            },
            "persist_result": persist_result,
            "au_financial_improved": improved,
            "classification_gap": (
                "discovery_or_ingestion"
                if not raw_markers.get("enough_money")
                else (
                    "extraction"
                    if raw_markers.get("enough_money")
                    and not extract_markers.get("enough_money")
                    else "none_for_financial_section"
                )
            ),
            "note": (
                "Immutable raw HTML preserved. Historical extraction JSON retained "
                "when content hash matches; upsert only on extractor/schema change."
            ),
        }
    finally:
        session.close()


def build_extraction_repairs_report(
    *,
    au_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "phase": "6F.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "improvements": [
            {
                "id": "au_pageschema_requirement_merge",
                "module": "lifepilot.extract.html_structured",
                "extractor_version": EXTRACTOR_VERSION,
                "change": (
                    "When main DOM is non-thin, merge embedded PageSchema/CMS HTML "
                    "blocks that add requirement-guidance markers absent from main "
                    "(e.g. financial capacity / gather documents)."
                ),
                "document_targeted": AU_STUDENT_DOC_ID,
                "result": au_result or {},
            }
        ],
        "not_extraction_gaps": [
            {
                "pathway_id": "sg_permanent_resident",
                "requirement": "required_documents",
                "reason": "Checklist PDF referenced but not present in raw corpus",
            },
            {
                "pathway_id": "nl_permanent_residence",
                "requirement": "required_documents",
                "reason": "Checklist absent from stored PR page content",
            },
            {
                "pathway_id": "au_student_500",
                "requirement": "required_documents",
                "reason": (
                    "Disclaimer/cost passages are not a document checklist; "
                    "substantive checklist not in inspected extractions"
                ),
            },
        ],
    }
