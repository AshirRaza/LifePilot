"""Bridge high-risk JSON queue → legal_review_items (Phase 10D.2).

Fail-closed:
- Dry-run by default.
- Never invent document_version_id or evidence fingerprints.
- Never treat JSON verification_status (including human_verified) as an approval.
- Idempotent on (subject_type=high_risk_queue_item, subject_ref=queue item id).
- Preserves the source JSON file (read-only).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.db.legal_review import ActorRef, create_pending_review_item, list_review_items
from lifepilot.db.models import Jurisdiction

DEFAULT_QUEUE_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "eval" / "high_risk_legal_review_queue.json"
)

# JSON operational statuses that must NEVER become LegalRule/human approvals.
LEGACY_STATUS_NON_APPROVAL = frozenset(
    {
        "requires_human_review",
        "automatically_checked",
        "human_verified",  # operational label only — not a DB approval
        "conflicting_evidence",
        "insufficient_evidence",
        "potentially_outdated",
    }
)


@dataclass
class ImportRecordResult:
    queue_item_id: str
    outcome: str  # imported | skipped | duplicate | quarantined | dry_run_would_import
    reason: str
    review_item_id: int | None = None
    public_id: str | None = None


@dataclass
class ImportSummary:
    queue_id: str | None
    dry_run: bool
    source_path: str
    imported: int = 0
    skipped: int = 0
    duplicate: int = 0
    quarantined: int = 0
    dry_run_would_import: int = 0
    records: list[ImportRecordResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "dry_run": self.dry_run,
            "source_path": self.source_path,
            "imported": self.imported,
            "skipped": self.skipped,
            "duplicate": self.duplicate,
            "quarantined": self.quarantined,
            "dry_run_would_import": self.dry_run_would_import,
            "records": [
                {
                    "queue_item_id": r.queue_item_id,
                    "outcome": r.outcome,
                    "reason": r.reason,
                    "review_item_id": r.review_item_id,
                    "public_id": r.public_id,
                }
                for r in self.records
            ],
        }


def load_high_risk_queue(path: Path | str | None = None) -> dict[str, Any]:
    queue_path = Path(path) if path is not None else DEFAULT_QUEUE_PATH
    raw = json.loads(queue_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("high-risk queue root must be an object")
    return raw


def import_high_risk_queue(
    session: Session,
    *,
    path: Path | str | None = None,
    dry_run: bool = True,
    actor: ActorRef | None = None,
    default_risk_class: str = "high",
    default_priority: str = "p1",
) -> ImportSummary:
    """Import/bridge queue items into pending review items.

    Items without ``document_version_id`` and without ``evidence_fingerprint``
    are quarantined (not imported). Optional provenance fields may appear in
    synthetic/test queue files only — never invented by this importer.
    """
    queue_path = Path(path) if path is not None else DEFAULT_QUEUE_PATH
    payload = load_high_risk_queue(queue_path)
    summary = ImportSummary(
        queue_id=str(payload.get("queue_id") or "") or None,
        dry_run=bool(dry_run),
        source_path=str(queue_path.resolve()),
    )
    service_actor = actor or ActorRef(
        actor_id="service:high_risk_queue_import",
        actor_kind="service",
    )
    items = payload.get("items") or []
    if not isinstance(items, list):
        raise ValueError("high-risk queue items must be a list")

    for raw in items:
        if not isinstance(raw, dict):
            summary.quarantined += 1
            summary.records.append(
                ImportRecordResult(
                    queue_item_id="<invalid>",
                    outcome="quarantined",
                    reason="queue item is not an object",
                )
            )
            continue
        result = _import_one(
            session,
            raw=raw,
            dry_run=dry_run,
            actor=service_actor,
            queue_id=summary.queue_id,
            source_path=summary.source_path,
            default_risk_class=default_risk_class,
            default_priority=default_priority,
        )
        summary.records.append(result)
        if result.outcome == "imported":
            summary.imported += 1
        elif result.outcome == "duplicate":
            summary.duplicate += 1
        elif result.outcome == "quarantined":
            summary.quarantined += 1
        elif result.outcome == "dry_run_would_import":
            summary.dry_run_would_import += 1
        else:
            summary.skipped += 1
    return summary


def _import_one(
    session: Session,
    *,
    raw: dict[str, Any],
    dry_run: bool,
    actor: ActorRef,
    queue_id: str | None,
    source_path: str,
    default_risk_class: str,
    default_priority: str,
) -> ImportRecordResult:
    item_id = str(raw.get("id") or "").strip()
    if not item_id:
        return ImportRecordResult(
            queue_item_id="<missing>",
            outcome="quarantined",
            reason="missing queue item id",
        )

    legacy_status = str(raw.get("verification_status") or "").strip()
    # Never promote legacy labels — even human_verified in JSON.
    if legacy_status and legacy_status not in LEGACY_STATUS_NON_APPROVAL:
        # Unknown statuses are quarantined rather than trusted.
        return ImportRecordResult(
            queue_item_id=item_id,
            outcome="quarantined",
            reason=f"unsupported legacy verification_status={legacy_status!r}",
        )

    jurisdiction_code = str(raw.get("jurisdiction") or "").strip().upper()
    pathway_id = str(raw.get("pathway_id") or "").strip() or None
    claim = str(raw.get("claim") or "").strip()
    if not jurisdiction_code or not claim:
        return ImportRecordResult(
            queue_item_id=item_id,
            outcome="quarantined",
            reason="missing jurisdiction or claim",
        )

    # Provenance: only accept explicitly provided pins; never invent.
    doc_ver_raw = raw.get("document_version_id")
    fingerprint = str(raw.get("evidence_fingerprint") or "").strip() or None
    document_version_id: int | None = None
    if doc_ver_raw is not None and str(doc_ver_raw).strip() != "":
        try:
            document_version_id = int(doc_ver_raw)
        except (TypeError, ValueError):
            return ImportRecordResult(
                queue_item_id=item_id,
                outcome="quarantined",
                reason="document_version_id is not an integer",
            )

    if document_version_id is None and not fingerprint:
        return ImportRecordResult(
            queue_item_id=item_id,
            outcome="quarantined",
            reason=(
                "insufficient evidence provenance: provide document_version_id "
                "or evidence_fingerprint (importer will not invent pins)"
            ),
        )

    jur = session.execute(
        select(Jurisdiction).where(Jurisdiction.code == jurisdiction_code)
    ).scalar_one_or_none()
    if jur is None:
        # Also try common country-style codes already seeded in DB.
        return ImportRecordResult(
            queue_item_id=item_id,
            outcome="quarantined",
            reason=f"jurisdiction {jurisdiction_code!r} not found in database",
        )

    existing = list_review_items(
        session,
        subject_type="high_risk_queue_item",
        subject_ref=item_id,
        limit=5,
    )
    if existing:
        return ImportRecordResult(
            queue_item_id=item_id,
            outcome="duplicate",
            reason="review item already exists for subject_ref",
            review_item_id=existing[0].id,
            public_id=str(existing[0].public_id),
        )

    reason = (
        f"Imported from high-risk queue {queue_id or 'unknown'} item {item_id}: {claim}"
    )
    metadata = {
        "import_origin": "high_risk_legal_review_queue",
        "queue_id": queue_id,
        "queue_item_id": item_id,
        "source_path": source_path,
        "legacy_verification_status": legacy_status or None,
        "legacy_verification_method": raw.get("verification_method"),
        "official_source_hint": raw.get("official_source_hint"),
        "legacy_reviewer": raw.get("reviewer"),
        "exceptions": raw.get("exceptions"),
        "uncertainty": raw.get("uncertainty"),
        # Explicit: JSON human_verified is operational only.
        "legacy_status_not_legal_approval": True,
    }

    if dry_run:
        return ImportRecordResult(
            queue_item_id=item_id,
            outcome="dry_run_would_import",
            reason="dry_run: would create pending review item (not an approval)",
        )

    item = create_pending_review_item(
        session,
        subject_type="high_risk_queue_item",
        jurisdiction_id=jur.id,
        subject_ref=item_id,
        pathway_id=pathway_id,
        requirement_category=str(raw.get("requirement_category") or "").strip() or None,
        risk_class=str(raw.get("risk_class") or default_risk_class).strip() or default_risk_class,
        priority=str(raw.get("priority") or default_priority).strip() or default_priority,
        document_version_id=document_version_id,
        evidence_fingerprint=fingerprint,
        review_reason=reason,
        actor=actor,
        event_rationale=reason,
        event_metadata=metadata,
    )
    return ImportRecordResult(
        queue_item_id=item_id,
        outcome="imported",
        reason="created pending review item; legacy status not treated as approval",
        review_item_id=item.id,
        public_id=str(item.public_id),
    )
