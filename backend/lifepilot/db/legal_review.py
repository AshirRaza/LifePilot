"""Persistence primitives for legal review governance (Phase 10D.1).

Fail-closed repository helpers:
- Create pending review items pinned to evidence versions.
- Append review events under transaction + optimistic concurrency.
- Read items and full event history.

This module does **not**:
- Promote LegalRule.verification_status to human_verified.
- Trust client-supplied reviewer IDs as authorization (callers must supply
  already-authenticated actor identifiers from server-side auth later).
- Expose HTTP approval endpoints.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session, joinedload

from lifepilot.db.models import (
    LEGAL_REVIEW_ACTOR_KINDS,
    LEGAL_REVIEW_EVENT_TYPES,
    LEGAL_REVIEW_PRIORITIES,
    LEGAL_REVIEW_RISK_CLASSES,
    LEGAL_REVIEW_SUBJECT_TYPES,
    LEGAL_REVIEW_WORKFLOW_STATES,
    DocumentVersion,
    LegalReviewEvent,
    LegalReviewItem,
    LegalRule,
)

# Intended workflow graph for future 10D.2 interactive transitions.
# 10D.1 validates against this graph when appending events.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    # source_version_changed → invalidated is fail-closed for any active item
    # whose pinned evidence may have changed (Phase 10D.3).
    "pending": frozenset(
        {"assigned", "in_review", "rejected", "superseded", "invalidated"}
    ),
    "assigned": frozenset(
        {"in_review", "pending", "rejected", "superseded", "invalidated"}
    ),
    "in_review": frozenset(
        {
            "approved",
            "rejected",
            "changes_requested",
            "superseded",
            "invalidated",
        }
    ),
    "changes_requested": frozenset(
        {"assigned", "in_review", "rejected", "superseded", "invalidated"}
    ),
    "approved": frozenset({"invalidated", "superseded", "reopened"}),
    "rejected": frozenset({"reopened", "superseded"}),
    "reopened": frozenset(
        {"assigned", "in_review", "superseded", "invalidated"}
    ),
    "invalidated": frozenset({"reopened", "superseded"}),
    "superseded": frozenset(),
}

EVENT_TYPE_TO_STATE: dict[str, str] = {
    "submitted_for_review": "pending",
    "assigned": "assigned",
    "review_started": "in_review",
    "approved": "approved",
    "rejected": "rejected",
    "changes_requested": "changes_requested",
    "reopened": "reopened",
    "approval_invalidated": "invalidated",
    "source_version_changed": "invalidated",
    "superseded": "superseded",
    # administrative_correction keeps/sets explicit to_state via argument
}

# Event types that assert a human legal decision and therefore require a human actor.
HUMAN_DECISION_EVENTS = frozenset(
    {"approved", "rejected", "changes_requested", "approval_invalidated"}
)


class LegalReviewError(ValueError):
    """Invalid review persistence request (fail-closed)."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _insert_review_event_row(
    session: Session,
    *,
    review_item_id: int,
    event_type: str,
    from_workflow_state: str | None,
    to_workflow_state: str,
    occurred_at: datetime,
    actor_id: str,
    actor_kind: str,
    rationale: str | None,
    evidence_fingerprint: str | None,
    document_version_id: int | None,
    event_metadata: dict[str, Any] | None,
) -> LegalReviewEvent:
    """Insert via gated SECURITY DEFINER function (Phase 10D.5).

    Direct ORM/SQL INSERT into ``legal_review_events`` is rejected by trigger
    unless ``current_user = lifepilot_gov_writer``. This helper is the only
    supported application write path.
    """
    import json

    new_id = session.execute(
        text(
            """
            SELECT lp_insert_legal_review_event(
                :review_item_id,
                :event_type,
                :from_workflow_state,
                :to_workflow_state,
                :occurred_at,
                :actor_id,
                :actor_kind,
                :rationale,
                :evidence_fingerprint,
                :document_version_id,
                CAST(:metadata AS jsonb)
            )
            """
        ),
        {
            "review_item_id": review_item_id,
            "event_type": event_type,
            "from_workflow_state": from_workflow_state,
            "to_workflow_state": to_workflow_state,
            "occurred_at": occurred_at,
            "actor_id": actor_id,
            "actor_kind": actor_kind,
            "rationale": rationale,
            "evidence_fingerprint": evidence_fingerprint,
            "document_version_id": document_version_id,
            "metadata": json.dumps(dict(event_metadata or {})),
        },
    ).scalar_one()
    event = session.get(LegalReviewEvent, int(new_id))
    if event is None:
        raise LegalReviewError(
            "failed to load inserted review event",
            code="event_insert_failed",
        )
    return event


@dataclass(frozen=True)
class ActorRef:
    """Server-side actor identity reference (not client-trusted by itself)."""

    actor_id: str
    actor_kind: str

    def __post_init__(self) -> None:
        aid = (self.actor_id or "").strip()
        kind = (self.actor_kind or "").strip()
        if not aid:
            raise LegalReviewError(
                "actor_id is required", code="missing_actor_id"
            )
        if kind not in LEGAL_REVIEW_ACTOR_KINDS:
            raise LegalReviewError(
                f"invalid actor_kind={kind!r}", code="invalid_actor_kind"
            )
        object.__setattr__(self, "actor_id", aid)
        object.__setattr__(self, "actor_kind", kind)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_in(value: str, allowed: Sequence[str], *, code: str, label: str) -> str:
    cleaned = (value or "").strip()
    if cleaned not in allowed:
        raise LegalReviewError(f"invalid {label}={value!r}", code=code)
    return cleaned


def create_pending_review_item(
    session: Session,
    *,
    subject_type: str,
    jurisdiction_id: int,
    review_reason: str,
    actor: ActorRef,
    legal_rule_id: int | None = None,
    subject_ref: str | None = None,
    pathway_id: str | None = None,
    requirement_category: str | None = None,
    risk_class: str = "high",
    priority: str = "p1",
    source_id: int | None = None,
    document_id: int | None = None,
    document_version_id: int | None = None,
    evidence_fingerprint: str | None = None,
    reviewed_effective_from: date | None = None,
    reviewed_effective_to: date | None = None,
    reviewed_cohort_key: str | None = None,
    review_deadline_at: datetime | None = None,
    reassessment_at: datetime | None = None,
    event_rationale: str | None = None,
    event_metadata: dict[str, Any] | None = None,
) -> LegalReviewItem:
    """Create a pending review item and append ``submitted_for_review``.

    Never mutates LegalRule.verification_status / review_status.
    """
    subject = _require_in(
        subject_type, LEGAL_REVIEW_SUBJECT_TYPES, code="invalid_subject_type", label="subject_type"
    )
    risk = _require_in(
        risk_class, LEGAL_REVIEW_RISK_CLASSES, code="invalid_risk_class", label="risk_class"
    )
    prio = _require_in(
        priority, LEGAL_REVIEW_PRIORITIES, code="invalid_priority", label="priority"
    )
    reason = (review_reason or "").strip()
    if not reason:
        raise LegalReviewError("review_reason is required", code="missing_review_reason")

    if subject == "legal_rule":
        if legal_rule_id is None:
            raise LegalReviewError(
                "legal_rule_id required for subject_type=legal_rule",
                code="missing_legal_rule_id",
            )
        if document_version_id is None:
            raise LegalReviewError(
                "document_version_id required to pin legal_rule reviews",
                code="missing_document_version_pin",
            )
        rule = session.get(LegalRule, legal_rule_id)
        if rule is None:
            raise LegalReviewError(
                f"legal_rule_id={legal_rule_id} not found",
                code="legal_rule_not_found",
            )
        if rule.jurisdiction_id != jurisdiction_id:
            raise LegalReviewError(
                "jurisdiction_id does not match LegalRule.jurisdiction_id",
                code="jurisdiction_mismatch",
            )
        # Pin must match rule's examined version when the rule already pins one.
        if (
            rule.document_version_id is not None
            and int(rule.document_version_id) != int(document_version_id)
        ):
            raise LegalReviewError(
                "document_version_id must match LegalRule.document_version_id",
                code="document_version_mismatch",
            )
    elif subject in {"claim_evidence", "high_risk_queue_item"}:
        if not (subject_ref or "").strip():
            raise LegalReviewError(
                "subject_ref required for non-legal_rule subjects",
                code="missing_subject_ref",
            )

    if document_version_id is None and not (evidence_fingerprint or "").strip():
        raise LegalReviewError(
            "document_version_id or evidence_fingerprint required",
            code="missing_evidence_pin",
        )

    if document_version_id is not None:
        ver = session.get(DocumentVersion, document_version_id)
        if ver is None:
            raise LegalReviewError(
                f"document_version_id={document_version_id} not found",
                code="document_version_not_found",
            )
        if document_id is None:
            document_id = ver.document_id
        elif int(document_id) != int(ver.document_id):
            raise LegalReviewError(
                "document_id does not match document_version.document_id",
                code="document_id_mismatch",
            )

    item = LegalReviewItem(
        public_id=uuid.uuid4(),
        subject_type=subject,
        legal_rule_id=legal_rule_id,
        subject_ref=(subject_ref or None) and subject_ref.strip(),
        jurisdiction_id=jurisdiction_id,
        pathway_id=(pathway_id or None) and pathway_id.strip() or None,
        requirement_category=requirement_category,
        risk_class=risk,
        workflow_state="pending",
        review_reason=reason,
        priority=prio,
        source_id=source_id,
        document_id=document_id,
        document_version_id=document_version_id,
        evidence_fingerprint=(evidence_fingerprint or None)
        and evidence_fingerprint.strip()
        or None,
        reviewed_effective_from=reviewed_effective_from,
        reviewed_effective_to=reviewed_effective_to,
        reviewed_cohort_key=reviewed_cohort_key,
        review_deadline_at=review_deadline_at,
        reassessment_at=reassessment_at,
        invalidated=False,
        row_version=1,
    )
    session.add(item)
    session.flush()

    _insert_review_event_row(
        session,
        review_item_id=item.id,
        event_type="submitted_for_review",
        from_workflow_state=None,
        to_workflow_state="pending",
        occurred_at=_utcnow(),
        actor_id=actor.actor_id,
        actor_kind=actor.actor_kind,
        rationale=event_rationale or reason,
        evidence_fingerprint=item.evidence_fingerprint,
        document_version_id=item.document_version_id,
        event_metadata=dict(event_metadata or {}),
    )
    return item


def get_review_item(
    session: Session,
    *,
    review_item_id: int | None = None,
    public_id: uuid.UUID | str | None = None,
    with_events: bool = False,
) -> LegalReviewItem | None:
    if review_item_id is None and public_id is None:
        raise LegalReviewError(
            "review_item_id or public_id required", code="missing_review_item_key"
        )
    stmt = select(LegalReviewItem)
    if with_events:
        stmt = stmt.options(joinedload(LegalReviewItem.events))
    if review_item_id is not None:
        stmt = stmt.where(LegalReviewItem.id == review_item_id)
    else:
        pid = public_id if isinstance(public_id, uuid.UUID) else uuid.UUID(str(public_id))
        stmt = stmt.where(LegalReviewItem.public_id == pid)
    return session.scalars(stmt).unique().first()


def list_review_items(
    session: Session,
    *,
    workflow_states: Iterable[str] | None = None,
    jurisdiction_id: int | None = None,
    legal_rule_id: int | None = None,
    pathway_id: str | None = None,
    risk_class: str | None = None,
    assigned_actor_id: str | None = None,
    subject_type: str | None = None,
    subject_ref: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> list[LegalReviewItem]:
    stmt = select(LegalReviewItem).order_by(LegalReviewItem.id.desc())
    if workflow_states is not None:
        states = [
            _require_in(s, LEGAL_REVIEW_WORKFLOW_STATES, code="invalid_workflow_state", label="workflow_state")
            for s in workflow_states
        ]
        stmt = stmt.where(LegalReviewItem.workflow_state.in_(states))
    if jurisdiction_id is not None:
        stmt = stmt.where(LegalReviewItem.jurisdiction_id == jurisdiction_id)
    if legal_rule_id is not None:
        stmt = stmt.where(LegalReviewItem.legal_rule_id == legal_rule_id)
    if pathway_id is not None:
        stmt = stmt.where(LegalReviewItem.pathway_id == pathway_id.strip())
    if risk_class is not None:
        risk = _require_in(
            risk_class, LEGAL_REVIEW_RISK_CLASSES, code="invalid_risk_class", label="risk_class"
        )
        stmt = stmt.where(LegalReviewItem.risk_class == risk)
    if assigned_actor_id is not None:
        stmt = stmt.where(LegalReviewItem.assigned_actor_id == assigned_actor_id.strip())
    if subject_type is not None:
        st = _require_in(
            subject_type, LEGAL_REVIEW_SUBJECT_TYPES, code="invalid_subject_type", label="subject_type"
        )
        stmt = stmt.where(LegalReviewItem.subject_type == st)
    if subject_ref is not None:
        stmt = stmt.where(LegalReviewItem.subject_ref == subject_ref.strip())
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(int(limit), 500))
    stmt = stmt.offset(safe_offset).limit(safe_limit)
    return list(session.scalars(stmt).all())


def get_review_history(
    session: Session,
    *,
    review_item_id: int,
) -> list[LegalReviewEvent]:
    stmt = (
        select(LegalReviewEvent)
        .where(LegalReviewEvent.review_item_id == review_item_id)
        .order_by(LegalReviewEvent.id.asc())
    )
    return list(session.scalars(stmt).all())


def append_review_event(
    session: Session,
    *,
    review_item_id: int,
    event_type: str,
    actor: ActorRef,
    expected_row_version: int,
    to_workflow_state: str | None = None,
    rationale: str | None = None,
    evidence_fingerprint: str | None = None,
    document_version_id: int | None = None,
    event_metadata: dict[str, Any] | None = None,
    allow_administrative_correction: bool = False,
    assigned_to: ActorRef | None = None,
) -> LegalReviewEvent:
    """Append an event and update item workflow state under row_version lock.

    Does **not** promote LegalRule to human_verified even for ``approved`` events.
    That promotion is reserved for a later phase with explicit authorization.
    """
    etype = _require_in(
        event_type, LEGAL_REVIEW_EVENT_TYPES, code="invalid_event_type", label="event_type"
    )
    if etype in HUMAN_DECISION_EVENTS and actor.actor_kind != "human_reviewer":
        raise LegalReviewError(
            f"event_type={etype!r} requires actor_kind=human_reviewer",
            code="human_actor_required",
        )
    if etype == "administrative_correction" and not allow_administrative_correction:
        raise LegalReviewError(
            "administrative_correction requires allow_administrative_correction=True",
            code="admin_correction_not_allowed",
        )

    # Lock the item row for concurrent safety.
    item = session.execute(
        select(LegalReviewItem)
        .where(LegalReviewItem.id == review_item_id)
        .with_for_update()
    ).scalar_one_or_none()
    if item is None:
        raise LegalReviewError(
            f"review_item_id={review_item_id} not found",
            code="review_item_not_found",
        )
    if int(item.row_version) != int(expected_row_version):
        raise LegalReviewError(
            "concurrent update: row_version mismatch",
            code="concurrent_update",
        )

    if etype == "administrative_correction":
        if not to_workflow_state:
            raise LegalReviewError(
                "to_workflow_state required for administrative_correction",
                code="missing_to_state",
            )
        target = _require_in(
            to_workflow_state,
            LEGAL_REVIEW_WORKFLOW_STATES,
            code="invalid_workflow_state",
            label="to_workflow_state",
        )
    else:
        target = EVENT_TYPE_TO_STATE.get(etype)
        if target is None:
            raise LegalReviewError(
                f"no default target state for event_type={etype!r}",
                code="missing_target_state",
            )
        if to_workflow_state is not None and to_workflow_state != target:
            raise LegalReviewError(
                f"to_workflow_state must be {target!r} for event_type={etype!r}",
                code="to_state_mismatch",
            )

    # submitted_for_review only valid at creation (from None); reject as append.
    if etype == "submitted_for_review":
        raise LegalReviewError(
            "submitted_for_review is only created with the review item",
            code="invalid_event_append",
        )

    from_state = item.workflow_state
    allowed = ALLOWED_TRANSITIONS.get(from_state, frozenset())
    if target not in allowed and not (
        etype == "administrative_correction" and allow_administrative_correction
    ):
        raise LegalReviewError(
            f"invalid transition {from_state!r} -> {target!r} via {etype!r}",
            code="invalid_workflow_transition",
        )

    pin_version = document_version_id if document_version_id is not None else item.document_version_id
    pin_fp = (
        (evidence_fingerprint or "").strip()
        or item.evidence_fingerprint
    )

    meta = dict(event_metadata or {})
    assignee_ref: ActorRef | None = None
    if etype == "assigned":
        assignee_ref = assigned_to or actor
        if assignee_ref.actor_kind != "human_reviewer":
            raise LegalReviewError(
                "assignee must be actor_kind=human_reviewer",
                code="invalid_assignee_kind",
            )
        meta.setdefault("assigned_to_actor_id", assignee_ref.actor_id)
        meta.setdefault("assigned_to_actor_kind", assignee_ref.actor_kind)
        meta.setdefault("assigned_by_actor_id", actor.actor_id)
        meta.setdefault("assigned_by_actor_kind", actor.actor_kind)

    event = _insert_review_event_row(
        session,
        review_item_id=item.id,
        event_type=etype,
        from_workflow_state=from_state,
        to_workflow_state=target,
        occurred_at=_utcnow(),
        actor_id=actor.actor_id,
        actor_kind=actor.actor_kind,
        rationale=(rationale or None) and rationale.strip() or None,
        evidence_fingerprint=pin_fp,
        document_version_id=pin_version,
        event_metadata=meta,
    )

    item.workflow_state = target
    item.row_version = int(item.row_version) + 1
    item.updated_at = _utcnow()

    if etype == "assigned" and assignee_ref is not None:
        item.assigned_actor_id = assignee_ref.actor_id
        item.assigned_actor_kind = assignee_ref.actor_kind
    if etype in {"approval_invalidated", "source_version_changed"}:
        item.invalidated = True
        item.invalidation_reason = (rationale or etype).strip()
        item.current_approved_event_id = None
    if etype == "approved":
        # Record pointer only — LegalRule promotion is an explicit 10D.4 operation.
        # Must set pointer before flush so 0014 trigger sees matching latest event.
        item.current_approved_event_id = event.id
        item.invalidated = False
        item.invalidation_reason = None
    if etype == "reopened":
        # Reopened items are again eligible for review; prior invalidation is historical.
        item.invalidated = False
        item.invalidation_reason = None
        item.current_approved_event_id = None
    if etype == "superseded":
        item.invalidated = True
        if not item.invalidation_reason:
            item.invalidation_reason = rationale or "superseded"

    session.flush()

    # Phase 10D.4: demote linked LegalRule authority when approval no longer holds.
    if (
        item.legal_rule_id is not None
        and etype
        in {
            "approval_invalidated",
            "source_version_changed",
            "reopened",
            "superseded",
            "rejected",
        }
    ):
        from lifepilot.governance.legal_rule_promotion import demote_if_authority_invalid

        demote_if_authority_invalid(
            session,
            legal_rule_id=item.legal_rule_id,
            reason=f"review_event:{etype}:event_id={event.id}",
        )

    return event


def assert_legal_rule_not_promoted_by_review(
    session: Session,
    *,
    legal_rule_id: int,
    expected_verification_status: str = "unverified",
) -> LegalRule:
    """Helper for tests/callers: review actions must not silently promote rules."""
    rule = session.get(LegalRule, legal_rule_id)
    if rule is None:
        raise LegalReviewError("legal_rule not found", code="legal_rule_not_found")
    if rule.verification_status != expected_verification_status:
        raise LegalReviewError(
            "LegalRule verification_status changed unexpectedly",
            code="unexpected_rule_promotion",
        )
    return rule
