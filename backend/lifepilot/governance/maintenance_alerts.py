"""Build operator maintenance alerts compatible with readiness probes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Maintenance pipeline statuses / change classes that indicate pending legal review.
_PENDING_STATUSES = frozenset({"pending_review", "queued_for_review"})
_PENDING_CHANGE_CLASSES = frozenset(
    {
        "potentially_material",
        "uncertain",
        "confirmed_material",
        "materiality_unknown",
    }
)
_FAILED_STATUSES = frozenset({"failed", "error", "fetch_failed"})


def is_pending_material_item(item: dict[str, Any]) -> bool:
    """True when a maintenance report/registry item needs operator attention.

    Fixes the legacy mismatch that required ``change_class.startswith("material")``
    and ``status == queued_for_review`` (pipeline emits ``potentially_material`` /
    ``pending_review``).
    """
    status = str(item.get("status") or "").strip().lower()
    change_class = str(item.get("change_class") or "").strip().lower()
    if status in _PENDING_STATUSES:
        return True
    if change_class in _PENDING_CHANGE_CLASSES:
        return True
    if change_class.startswith("material"):
        return True
    return False


def is_failed_item(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "").strip().lower()
    change_class = str(item.get("change_class") or "").strip().lower()
    return status in _FAILED_STATUSES or change_class == "fetch_failed"


def build_freshness_alerts(
    *,
    report_items: list[dict[str, Any]] | None = None,
    pending_registry_items: list[dict[str, Any]] | None = None,
    invalidation_records: list[dict[str, Any]] | None = None,
    reassessment_entries: list[dict[str, Any]] | None = None,
    provenance_gaps: list[dict[str, Any]] | None = None,
    counts: dict[str, Any] | None = None,
    dry_run: bool | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Compose ``freshness_alerts.json`` payload without uncontrolled duplicates.

    Dedupes pending material items by stable ``id`` / document+version keys.
    Alert text must not claim conclusive legal invalidity for potential changes.
    """
    pending_map: dict[str, dict[str, Any]] = {}
    for src in (report_items or []) + (pending_registry_items or []):
        if not is_pending_material_item(src):
            continue
        key = str(
            src.get("id")
            or f"doc:{src.get('document_id')}:new:{src.get('new_version_id') or src.get('version_id')}"
        )
        pending_map[key] = {
            "id": key,
            "document_id": src.get("document_id"),
            "source_id": src.get("source_id"),
            "previous_version_id": src.get("previous_version_id"),
            "new_version_id": src.get("new_version_id") or src.get("version_id"),
            "change_class": src.get("change_class"),
            "status": src.get("status"),
            "jurisdiction": src.get("jurisdiction"),
            "pathway_id": src.get("pathway_id"),
            "canonical_url": src.get("canonical_url"),
            "severity": "high",
            "note": (
                "Potential or unknown material source change — not a conclusive "
                "finding that immigration law changed; human review required."
            ),
        }

    failed = [i for i in (report_items or []) if is_failed_item(i)]

    invalidations = []
    for row in invalidation_records or []:
        if row.get("outcome") != "invalidated":
            continue
        invalidations.append(
            {
                "review_item_id": row.get("review_item_id"),
                "event_id": row.get("event_id"),
                "materiality": row.get("materiality"),
                "severity": "critical",
                "note": row.get("reason"),
            }
        )

    overdue = [
        e
        for e in (reassessment_entries or [])
        if e.get("urgency") in {"overdue", "urgent"}
    ]
    missing_prov = list(provenance_gaps or []) + [
        e for e in (reassessment_entries or []) if e.get("urgency") == "provenance"
    ]

    return {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "overdue_sources": failed,
        "pending_material_changes": list(pending_map.values()),
        "high_risk_review_invalidations": invalidations,
        "overdue_reassessments": overdue,
        "missing_provenance": missing_prov,
        "counts": counts or {
            "pending_material": len(pending_map),
            "failed": len(failed),
            "invalidations": len(invalidations),
            "overdue_reassessments": len(overdue),
            "missing_provenance": len(missing_prov),
        },
    }
