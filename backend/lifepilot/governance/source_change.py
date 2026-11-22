"""Source-version change → legal review invalidation (Phase 10D.3).

A webpage update is not automatically a legal change. Classifications are
conservative and explainable. Approvals are never auto-restored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.db.legal_review import (
    ActorRef,
    LegalReviewError,
    append_review_event,
    get_review_history,
)
from lifepilot.db.models import DocumentVersion, LegalReviewItem
from lifepilot.maintenance.change_classify import ChangeClass, classify_content_change

SERVICE_ACTOR = ActorRef(
    actor_id="service:source_change_monitor",
    actor_kind="service",
)

HIGH_RISK_CLASSES = frozenset({"critical", "high"})


class SourceChangeMateriality(str, Enum):
    """Deterministic materiality labels for governance decisions."""

    NO_MEANINGFUL_CONTENT_CHANGE = "no_meaningful_content_change"
    METADATA_ONLY = "metadata_only"
    NONMATERIAL_CONTENT_CHANGE = "nonmaterial_content_change"
    POTENTIALLY_MATERIAL = "potentially_material"
    CONFIRMED_MATERIAL = "confirmed_material"
    MATERIALITY_UNKNOWN = "materiality_unknown"


# Fail-closed: these trigger invalidation for high-risk approved items.
INVALIDATING_MATERIALITIES = frozenset(
    {
        SourceChangeMateriality.POTENTIALLY_MATERIAL,
        SourceChangeMateriality.CONFIRMED_MATERIAL,
        SourceChangeMateriality.MATERIALITY_UNKNOWN,
    }
)


def classify_source_change_materiality(
    *,
    previous_text: str | None = None,
    new_text: str | None = None,
    fetch_ok: bool = True,
    content_hashes_equal: bool | None = None,
    http_metadata_only: bool = False,
    structured_comparison_status: str | None = None,
    change_class: str | ChangeClass | None = None,
) -> SourceChangeMateriality:
    """Map crawl/comparison signals to an explainable materiality label.

    Policy notes:
    - ETag/Last-Modified alone → METADATA_ONLY (never invalidates).
    - Equal content hashes → NO_MEANINGFUL_CONTENT_CHANGE.
    - ``ChangeClass.EDITORIAL`` / structured unchanged → NONMATERIAL.
    - ``ChangeClass.POTENTIALLY_MATERIAL`` → POTENTIALLY_MATERIAL.
    - Structured ``changed`` + material heuristic → CONFIRMED_MATERIAL.
    - Otherwise uncertain → MATERIALITY_UNKNOWN.
    """
    if http_metadata_only and content_hashes_equal is not False:
        return SourceChangeMateriality.METADATA_ONLY
    if content_hashes_equal is True:
        return SourceChangeMateriality.NO_MEANINGFUL_CONTENT_CHANGE

    cc: ChangeClass | None = None
    if isinstance(change_class, ChangeClass):
        cc = change_class
    elif isinstance(change_class, str) and change_class.strip():
        try:
            cc = ChangeClass(change_class.strip())
        except ValueError:
            cc = None
    if cc is None and (previous_text is not None or new_text is not None):
        cc = classify_content_change(
            previous_text=previous_text,
            new_text=new_text,
            fetch_ok=fetch_ok,
            hashes_equal=content_hashes_equal,
        )

    structured = (structured_comparison_status or "").strip().lower()
    if cc == ChangeClass.UNCHANGED and structured in {"", "unchanged"}:
        return SourceChangeMateriality.NO_MEANINGFUL_CONTENT_CHANGE
    if cc == ChangeClass.EDITORIAL and structured != "changed":
        return SourceChangeMateriality.NONMATERIAL_CONTENT_CHANGE
    if cc == ChangeClass.FETCH_FAILED:
        return SourceChangeMateriality.MATERIALITY_UNKNOWN
    if cc == ChangeClass.POTENTIALLY_MATERIAL:
        if structured == "changed":
            return SourceChangeMateriality.CONFIRMED_MATERIAL
        return SourceChangeMateriality.POTENTIALLY_MATERIAL
    if cc == ChangeClass.UNCERTAIN:
        return SourceChangeMateriality.MATERIALITY_UNKNOWN
    if structured == "changed":
        # Structured extraction changed but text heuristic was editorial/unknown.
        return SourceChangeMateriality.POTENTIALLY_MATERIAL
    if structured == "unavailable":
        return SourceChangeMateriality.MATERIALITY_UNKNOWN
    if cc == ChangeClass.UNCHANGED:
        return SourceChangeMateriality.NO_MEANINGFUL_CONTENT_CHANGE
    return SourceChangeMateriality.MATERIALITY_UNKNOWN


def should_invalidate_review_item(
    item: LegalReviewItem,
    materiality: SourceChangeMateriality,
    *,
    invalidate_low_risk_on_potential: bool = False,
) -> bool:
    """Decide whether a review item must be invalidated for this change."""
    if materiality not in INVALIDATING_MATERIALITIES:
        return False
    risk = (item.risk_class or "high").strip().lower()
    if risk in HIGH_RISK_CLASSES:
        return True
    if materiality == SourceChangeMateriality.CONFIRMED_MATERIAL:
        return True
    if risk == "medium" and materiality in {
        SourceChangeMateriality.POTENTIALLY_MATERIAL,
        SourceChangeMateriality.MATERIALITY_UNKNOWN,
    }:
        return True
    if risk == "low":
        return bool(
            invalidate_low_risk_on_potential
            or materiality == SourceChangeMateriality.CONFIRMED_MATERIAL
        )
    return True  # unknown risk → fail closed


@dataclass
class InvalidationRecord:
    review_item_id: int
    outcome: str  # invalidated | skipped | already_invalidated | error
    reason: str
    materiality: str | None = None
    event_id: int | None = None
    previous_workflow_state: str | None = None


@dataclass
class SourceChangeInvalidationResult:
    previous_version_id: int | None
    new_version_id: int
    document_id: int | None
    materiality: SourceChangeMateriality
    dry_run: bool
    records: list[InvalidationRecord] = field(default_factory=list)

    @property
    def invalidated_count(self) -> int:
        return sum(1 for r in self.records if r.outcome == "invalidated")

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.records if r.outcome == "skipped")

    @property
    def already_count(self) -> int:
        return sum(1 for r in self.records if r.outcome == "already_invalidated")

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_version_id": self.previous_version_id,
            "new_version_id": self.new_version_id,
            "document_id": self.document_id,
            "materiality": self.materiality.value,
            "dry_run": self.dry_run,
            "invalidated": self.invalidated_count,
            "skipped": self.skipped_count,
            "already_invalidated": self.already_count,
            "records": [
                {
                    "review_item_id": r.review_item_id,
                    "outcome": r.outcome,
                    "reason": r.reason,
                    "materiality": r.materiality,
                    "event_id": r.event_id,
                    "previous_workflow_state": r.previous_workflow_state,
                }
                for r in self.records
            ],
        }


def find_review_items_pinned_to_version(
    session: Session,
    *,
    document_version_id: int,
) -> list[LegalReviewItem]:
    """Review items whose examined evidence pin is this DocumentVersion."""
    stmt = (
        select(LegalReviewItem)
        .where(LegalReviewItem.document_version_id == document_version_id)
        .order_by(LegalReviewItem.id.asc())
    )
    return list(session.scalars(stmt).all())


def _already_invalidated_for_new_version(
    session: Session, item: LegalReviewItem, new_version_id: int
) -> bool:
    if not item.invalidated and item.workflow_state != "invalidated":
        return False
    hist = get_review_history(session, review_item_id=item.id)
    for event in reversed(hist):
        if event.event_type != "source_version_changed":
            continue
        meta = event.event_metadata or {}
        if int(meta.get("new_document_version_id") or 0) == int(new_version_id):
            return True
        if event.document_version_id is not None and int(
            event.document_version_id
        ) == int(new_version_id):
            return True
    return False


def process_source_version_change(
    session: Session,
    *,
    previous_version_id: int | None,
    new_version_id: int,
    materiality: SourceChangeMateriality | None = None,
    previous_text: str | None = None,
    new_text: str | None = None,
    content_hashes_equal: bool | None = None,
    http_metadata_only: bool = False,
    structured_comparison_status: str | None = None,
    change_class: str | ChangeClass | None = None,
    dry_run: bool = True,
    actor: ActorRef | None = None,
    invalidate_low_risk_on_potential: bool = False,
) -> SourceChangeInvalidationResult:
    """Invalidate affected review items for a new DocumentVersion.

    Idempotent for the same ``new_version_id``. Does not mutate LegalRule rows.
    Does not invent approvals. Dry-run performs zero writes.
    """
    new_ver = session.get(DocumentVersion, new_version_id)
    if new_ver is None:
        raise LegalReviewError(
            f"new_version_id={new_version_id} not found",
            code="document_version_not_found",
        )
    if previous_version_id is not None:
        prev = session.get(DocumentVersion, previous_version_id)
        if prev is None:
            raise LegalReviewError(
                f"previous_version_id={previous_version_id} not found",
                code="document_version_not_found",
            )
        if int(prev.document_id) != int(new_ver.document_id):
            raise LegalReviewError(
                "previous and new versions belong to different documents",
                code="document_mismatch",
            )

    mat = materiality or classify_source_change_materiality(
        previous_text=previous_text,
        new_text=new_text,
        content_hashes_equal=content_hashes_equal,
        http_metadata_only=http_metadata_only,
        structured_comparison_status=structured_comparison_status,
        change_class=change_class,
    )
    result = SourceChangeInvalidationResult(
        previous_version_id=previous_version_id,
        new_version_id=new_version_id,
        document_id=new_ver.document_id,
        materiality=mat,
        dry_run=dry_run,
    )

    # Only items pinned to the *previous* examined version are candidates.
    # Avoid jurisdiction-wide invalidation.
    pin_id = previous_version_id
    if pin_id is None:
        return result

    items = find_review_items_pinned_to_version(session, document_version_id=pin_id)
    svc_actor = actor or SERVICE_ACTOR

    for item in items:
        if item.workflow_state == "superseded":
            result.records.append(
                InvalidationRecord(
                    review_item_id=item.id,
                    outcome="skipped",
                    reason="item superseded",
                    materiality=mat.value,
                    previous_workflow_state=item.workflow_state,
                )
            )
            continue

        if _already_invalidated_for_new_version(session, item, new_version_id):
            result.records.append(
                InvalidationRecord(
                    review_item_id=item.id,
                    outcome="already_invalidated",
                    reason="already recorded source_version_changed for this new version",
                    materiality=mat.value,
                    previous_workflow_state=item.workflow_state,
                )
            )
            continue

        if not should_invalidate_review_item(
            item,
            mat,
            invalidate_low_risk_on_potential=invalidate_low_risk_on_potential,
        ):
            result.records.append(
                InvalidationRecord(
                    review_item_id=item.id,
                    outcome="skipped",
                    reason=f"materiality={mat.value} does not require invalidation "
                    f"for risk_class={item.risk_class}",
                    materiality=mat.value,
                    previous_workflow_state=item.workflow_state,
                )
            )
            continue

        rationale = (
            f"Source DocumentVersion change may affect reviewed evidence "
            f"(previous={previous_version_id}, new={new_version_id}, "
            f"materiality={mat.value}). Not a conclusive finding that the law "
            f"changed; human reassessment required."
        )
        if dry_run:
            result.records.append(
                InvalidationRecord(
                    review_item_id=item.id,
                    outcome="skipped",
                    reason=f"dry_run: would invalidate ({rationale})",
                    materiality=mat.value,
                    previous_workflow_state=item.workflow_state,
                )
            )
            continue

        try:
            event = append_review_event(
                session,
                review_item_id=item.id,
                event_type="source_version_changed",
                actor=svc_actor,
                expected_row_version=int(item.row_version),
                rationale=rationale,
                document_version_id=new_version_id,
                event_metadata={
                    "operation": "source_version_changed",
                    "previous_document_version_id": previous_version_id,
                    "new_document_version_id": new_version_id,
                    "document_id": new_ver.document_id,
                    "materiality": mat.value,
                    "risk_class": item.risk_class,
                    "not_legal_conclusion": True,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            result.records.append(
                InvalidationRecord(
                    review_item_id=item.id,
                    outcome="invalidated",
                    reason=rationale,
                    materiality=mat.value,
                    event_id=event.id,
                    previous_workflow_state=event.from_workflow_state,
                )
            )
        except LegalReviewError as exc:
            result.records.append(
                InvalidationRecord(
                    review_item_id=item.id,
                    outcome="error",
                    reason=f"{exc.code}: {exc}",
                    materiality=mat.value,
                    previous_workflow_state=item.workflow_state,
                )
            )
    return result


def find_stale_pinned_review_items(session: Session) -> list[LegalReviewItem]:
    """Items pinned to a DocumentVersion that is no longer ``is_current``."""
    stmt = (
        select(LegalReviewItem)
        .join(
            DocumentVersion,
            DocumentVersion.id == LegalReviewItem.document_version_id,
        )
        .where(
            LegalReviewItem.document_version_id.is_not(None),
            DocumentVersion.is_current.is_(False),
            LegalReviewItem.workflow_state.notin_(["superseded", "invalidated"]),
        )
        .order_by(LegalReviewItem.id.asc())
    )
    return list(session.scalars(stmt).all())
