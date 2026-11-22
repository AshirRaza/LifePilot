"""Fail-closed review-pin guard when a new DocumentVersion becomes current.

Closes the ordinary-ingestion bypass: any genuine new current version that
affects pinned legal-review evidence must run shared source-change processing
in the same transaction. Classification failure → materiality_unknown (fail closed).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from lifepilot.governance.source_change import (
    SourceChangeMateriality,
    classify_source_change_materiality,
    find_review_items_pinned_to_version,
    process_source_version_change,
)
from lifepilot.maintenance.pending_changes import upsert_pending_item

logger = logging.getLogger("lifepilot.governance.ingest_version_guard")


def enforce_review_pins_on_new_current(
    session: Session,
    *,
    previous_version_id: int | None,
    new_version_id: int,
    document_id: int | None = None,
    content_hashes_equal: bool = False,
    previous_text: str | None = None,
    new_text: str | None = None,
    structured_comparison_status: str | None = None,
    http_metadata_only: bool = False,
) -> dict[str, Any]:
    """Run after ``set_current_version`` inside the same open transaction.

    - No-op / metadata-only / nonmaterial: may skip invalidation per policy.
    - Potentially material / unknown / confirmed: invalidate affected pins.
    - On unexpected errors: re-raise so the fetch/ingest transaction rolls back
      (prevents a new current version with silently usable stale approvals).
    """
    if previous_version_id is None:
        return {"skipped": True, "reason": "no_previous_version"}
    if content_hashes_equal or http_metadata_only:
        mat = classify_source_change_materiality(
            content_hashes_equal=True if content_hashes_equal else None,
            http_metadata_only=http_metadata_only,
        )
        if mat in {
            SourceChangeMateriality.NO_MEANINGFUL_CONTENT_CHANGE,
            SourceChangeMateriality.METADATA_ONLY,
        }:
            return {"skipped": True, "reason": mat.value, "materiality": mat.value}

    pinned = find_review_items_pinned_to_version(
        session, document_version_id=previous_version_id
    )
    if not pinned:
        return {"skipped": True, "reason": "no_pinned_review_items"}

    try:
        mat = classify_source_change_materiality(
            previous_text=previous_text,
            new_text=new_text,
            content_hashes_equal=False,
            structured_comparison_status=structured_comparison_status,
            http_metadata_only=False,
        )
    except Exception:  # noqa: BLE001
        logger.exception("ingest_version_guard.classify_failed")
        mat = SourceChangeMateriality.MATERIALITY_UNKNOWN

    result = process_source_version_change(
        session,
        previous_version_id=previous_version_id,
        new_version_id=new_version_id,
        materiality=mat,
        dry_run=False,
    )

    # Durable operator visibility when invalidation applied or materiality unknown.
    if result.invalidated_count or mat in {
        SourceChangeMateriality.POTENTIALLY_MATERIAL,
        SourceChangeMateriality.CONFIRMED_MATERIAL,
        SourceChangeMateriality.MATERIALITY_UNKNOWN,
    }:
        upsert_pending_item(
            {
                "id": f"ingest_chg_{document_id or 'doc'}_{new_version_id}",
                "document_id": document_id,
                "previous_version_id": previous_version_id,
                "new_version_id": new_version_id,
                "change_class": mat.value,
                "status": "pending_review",
                "claim": (
                    "Ingest/crawl promoted a new DocumentVersion that may affect "
                    "pinned legal-review evidence (not a conclusive legal finding)."
                ),
                "origin": "ingest_version_guard",
            }
        )

    # Demote LegalRules whose authority no longer validates.
    from lifepilot.governance.legal_rule_promotion import demote_if_authority_invalid

    for rec in result.records:
        if rec.outcome != "invalidated":
            continue
        item = next((i for i in pinned if i.id == rec.review_item_id), None)
        if item is not None and item.legal_rule_id is not None:
            demote_if_authority_invalid(
                session,
                legal_rule_id=item.legal_rule_id,
                reason=f"source_version_changed:event={rec.event_id}",
            )

    return result.to_dict()
