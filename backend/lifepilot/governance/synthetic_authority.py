"""Synthetic human-authority attachment for isolated tests (Phase 10D.4).

Creates a real approved review event chain and aligns LegalRule audit fields.
Never invents production approvals. Not an API surface.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from lifepilot.db.legal_review import (
    ActorRef,
    append_review_event,
    create_pending_review_item,
)
from lifepilot.db.models import LegalRule
from lifepilot.governance.legal_rule_promotion import promote_legal_rule_from_approval
from lifepilot.governance.reviewer_auth import (
    PERM_LEGAL_REVIEW_ADMIN,
    PERM_LEGAL_REVIEW_ASSIGN,
    PERM_LEGAL_REVIEW_DECIDE,
    PERM_LEGAL_REVIEW_READ,
    ReviewerIdentity,
)


def ensure_synthetic_human_authority(
    session: Session,
    rule: LegalRule,
    *,
    actor_id: str = "synthetic_reviewer",
    review_reason: str = "Synthetic isolated test approval chain.",
) -> LegalRule:
    """Attach review item + approval event and promote ``rule`` to human_verified.

    ``rule`` must already be persisted with ``document_version_id`` set.
    Starts from unverified/unreviewed if needed.
    """
    if rule.id is None:
        raise ValueError("rule must be flushed before ensure_synthetic_human_authority")
    if rule.document_version_id is None:
        raise ValueError("rule.document_version_id is required")

    # Ensure rule is not already human_verified without linkage (trigger would block).
    if rule.verification_status == "human_verified":
        rule.verification_status = "unverified"
        rule.review_status = "unreviewed"
        session.flush()

    actor = ActorRef(actor_id=actor_id, actor_kind="human_reviewer")
    item = create_pending_review_item(
        session,
        subject_type="legal_rule",
        jurisdiction_id=rule.jurisdiction_id,
        legal_rule_id=rule.id,
        document_version_id=rule.document_version_id,
        document_id=rule.document_id,
        source_id=rule.source_id,
        pathway_id=rule.pathway_id,
        requirement_category=rule.requirement_category,
        review_reason=review_reason,
        actor=ActorRef(actor_id="service:synthetic_seed", actor_kind="service"),
        reviewed_cohort_key=rule.cohort_key,
        reviewed_effective_from=rule.effective_from,
        reviewed_effective_to=rule.effective_to,
        risk_class="high",
    )
    append_review_event(
        session,
        review_item_id=item.id,
        event_type="review_started",
        actor=actor,
        expected_row_version=1,
    )
    append_review_event(
        session,
        review_item_id=item.id,
        event_type="approved",
        actor=actor,
        expected_row_version=2,
        rationale=review_reason,
        document_version_id=rule.document_version_id,
        event_metadata={"reviewed_rule_revision": int(rule.rule_revision or 1)},
    )
    session.refresh(item)
    identity = ReviewerIdentity(
        actor_id=actor_id,
        actor_kind="human_reviewer",
        permissions=frozenset(
            {
                PERM_LEGAL_REVIEW_READ,
                PERM_LEGAL_REVIEW_ASSIGN,
                PERM_LEGAL_REVIEW_DECIDE,
                PERM_LEGAL_REVIEW_ADMIN,
            }
        ),
    )
    return promote_legal_rule_from_approval(
        session,
        review_item_id=item.id,
        identity=identity,
        expected_item_row_version=int(item.row_version),
        rationale=review_reason,
    )
