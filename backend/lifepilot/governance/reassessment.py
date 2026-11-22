"""Deterministic reassessment queue for legal review items (Phase 10D.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.db.models import LegalReviewItem

HIGH_RISK = frozenset({"critical", "high"})
APPROACHING_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class ReassessmentEntry:
    review_item_id: int
    public_id: str
    workflow_state: str
    risk_class: str
    priority: str
    reason: str
    urgency: str  # urgent | due | overdue | provenance
    reassessment_at: str | None
    review_deadline_at: str | None
    legal_rule_id: int | None
    document_version_id: int | None
    invalidated: bool


@dataclass
class ReassessmentQueue:
    generated_at: str
    as_of: str
    entries: list[ReassessmentEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "as_of": self.as_of,
            "counts": {
                "total": len(self.entries),
                "urgent": sum(1 for e in self.entries if e.urgency == "urgent"),
                "overdue": sum(1 for e in self.entries if e.urgency == "overdue"),
                "due": sum(1 for e in self.entries if e.urgency == "due"),
                "provenance": sum(1 for e in self.entries if e.urgency == "provenance"),
            },
            "entries": [
                {
                    "review_item_id": e.review_item_id,
                    "public_id": e.public_id,
                    "workflow_state": e.workflow_state,
                    "risk_class": e.risk_class,
                    "priority": e.priority,
                    "reason": e.reason,
                    "urgency": e.urgency,
                    "reassessment_at": e.reassessment_at,
                    "review_deadline_at": e.review_deadline_at,
                    "legal_rule_id": e.legal_rule_id,
                    "document_version_id": e.document_version_id,
                    "invalidated": e.invalidated,
                }
                for e in self.entries
            ],
        }


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def build_reassessment_queue(
    session: Session,
    *,
    as_of: datetime | None = None,
    jurisdiction_id: int | None = None,
    pathway_id: str | None = None,
    include_states: Iterable[str] | None = None,
) -> ReassessmentQueue:
    """Identify due / overdue / invalidated / provenance-gap review items.

    Deadlines are never silently extended. Time comparison uses UTC.
    Duplicate queue rows are avoided by emitting one entry per item with the
    highest-urgency applicable reason.
    """
    now = _aware(as_of) or datetime.now(timezone.utc)
    states = list(
        include_states
        or (
            "pending",
            "assigned",
            "in_review",
            "changes_requested",
            "approved",
            "reopened",
            "invalidated",
            "rejected",
        )
    )
    stmt = (
        select(LegalReviewItem)
        .where(LegalReviewItem.workflow_state.in_(states))
        .order_by(LegalReviewItem.id.asc())
    )
    if jurisdiction_id is not None:
        stmt = stmt.where(LegalReviewItem.jurisdiction_id == jurisdiction_id)
    if pathway_id is not None:
        stmt = stmt.where(LegalReviewItem.pathway_id == pathway_id.strip())

    entries: list[ReassessmentEntry] = []
    for item in session.scalars(stmt).all():
        reasons: list[tuple[str, str]] = []  # (urgency, reason)

        if item.invalidated or item.workflow_state == "invalidated":
            urg = "urgent" if (item.risk_class or "") in HIGH_RISK else "due"
            reasons.append(
                (
                    urg,
                    "invalidated_by_source_change_or_approval_invalidation",
                )
            )

        deadline = _aware(item.review_deadline_at)
        reassess = _aware(item.reassessment_at)
        for label, ts in (
            ("review_deadline_at", deadline),
            ("reassessment_at", reassess),
        ):
            if ts is None:
                continue
            if ts < now:
                reasons.append(("overdue", f"overdue_{label}"))
            elif ts <= now + APPROACHING_WINDOW:
                reasons.append(("due", f"due_or_approaching_{label}"))

        if item.document_version_id is None and not (
            item.evidence_fingerprint or ""
        ).strip():
            reasons.append(("provenance", "missing_evidence_provenance"))

        if not reasons:
            continue

        urgency_rank = {"urgent": 0, "overdue": 1, "due": 2, "provenance": 3}
        reasons.sort(key=lambda r: urgency_rank.get(r[0], 9))
        top_urgency, top_reason = reasons[0]
        # Merge additional reasons into reason string for auditability.
        extra = [r for _, r in reasons[1:]]
        reason = top_reason if not extra else f"{top_reason};" + ";".join(extra)

        entries.append(
            ReassessmentEntry(
                review_item_id=item.id,
                public_id=str(item.public_id),
                workflow_state=item.workflow_state,
                risk_class=item.risk_class,
                priority=item.priority,
                reason=reason,
                urgency=top_urgency,
                reassessment_at=reassess.isoformat() if reassess else None,
                review_deadline_at=deadline.isoformat() if deadline else None,
                legal_rule_id=item.legal_rule_id,
                document_version_id=item.document_version_id,
                invalidated=bool(item.invalidated),
            )
        )

    # Stable ordering: urgency then id.
    entries.sort(key=lambda e: (urgency_rank.get(e.urgency, 9), e.review_item_id))
    return ReassessmentQueue(
        generated_at=datetime.now(timezone.utc).isoformat(),
        as_of=now.isoformat(),
        entries=entries,
    )
