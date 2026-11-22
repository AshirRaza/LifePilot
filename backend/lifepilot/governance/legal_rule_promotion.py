"""Authorized LegalRule promotion / demotion (Phase 10D.4).

Promotion is never performed by ingestion, LLM output, queue import, or
service API keys. Requires an authenticated human reviewer identity and an
existing approved review event.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.db.legal_review import LegalReviewError, get_review_item
from lifepilot.db.models import LegalReviewEvent, LegalReviewItem, LegalRule
from lifepilot.governance.approval_invariant import validate_legal_rule_human_authority
from lifepilot.governance.reviewer_auth import (
    PERM_LEGAL_REVIEW_DECIDE,
    ReviewerAuthError,
    ReviewerIdentity,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def promote_legal_rule_from_approval(
    session: Session,
    *,
    review_item_id: int,
    identity: ReviewerIdentity,
    expected_item_row_version: int,
    rationale: str | None = None,
) -> LegalRule:
    """Promote the linked LegalRule to human_verified from an approved review item.

    Transactional: locks item + rule; sets LegalRule audit fields to match the
    approving event. Does not create a new approval event.
    """
    identity.require_permission(PERM_LEGAL_REVIEW_DECIDE)
    if identity.actor_kind != "human_reviewer":
        raise ReviewerAuthError(
            "only human reviewers may promote LegalRule verification",
            code="human_actor_required",
        )
    reason = (rationale or "").strip()
    if not reason:
        # Prefer event rationale; still require an explicit promotion rationale.
        raise LegalReviewError(
            "promotion rationale is required", code="missing_rationale"
        )

    item = session.execute(
        select(LegalReviewItem)
        .where(LegalReviewItem.id == review_item_id)
        .with_for_update()
    ).scalar_one_or_none()
    if item is None:
        raise LegalReviewError("review item not found", code="review_item_not_found")
    if int(item.row_version) != int(expected_item_row_version):
        raise LegalReviewError(
            "concurrent update: review item row_version mismatch",
            code="concurrent_update",
        )
    if item.workflow_state != "approved" or item.invalidated:
        raise LegalReviewError(
            "review item must be approved and non-invalidated",
            code="review_item_not_approved",
        )
    if item.legal_rule_id is None:
        raise LegalReviewError(
            "review item has no linked LegalRule", code="missing_legal_rule_id"
        )
    if item.current_approved_event_id is None:
        raise LegalReviewError(
            "missing current_approved_event_id", code="missing_approval_event"
        )

    event = session.get(LegalReviewEvent, item.current_approved_event_id)
    if event is None or event.event_type != "approved":
        raise LegalReviewError(
            "approving event missing or not approved", code="missing_approval_event"
        )
    if event.actor_kind != "human_reviewer" or not (event.actor_id or "").strip():
        raise LegalReviewError(
            "approving event lacks human reviewer", code="human_actor_required"
        )
    # Promoter must be the approving actor or an admin with decide+admin.
    if event.actor_id != identity.actor_id and not identity.has_permission(
        "legal_review.admin"
    ):
        raise ReviewerAuthError(
            "promoter must match approving reviewer (or hold admin)",
            code="permission_denied",
        )

    rule = session.execute(
        select(LegalRule).where(LegalRule.id == item.legal_rule_id).with_for_update()
    ).scalar_one_or_none()
    if rule is None:
        raise LegalReviewError("legal_rule not found", code="legal_rule_not_found")

    if item.document_version_id is None:
        raise LegalReviewError(
            "review item missing document_version pin",
            code="missing_document_version_pin",
        )
    # Bind promotion to the pinned examined version (replay onto new revision forbidden).
    rule.document_version_id = item.document_version_id
    if item.document_id is not None:
        rule.document_id = item.document_id
    if item.source_id is not None:
        rule.source_id = item.source_id

    if item.pathway_id and rule.pathway_id and item.pathway_id != rule.pathway_id:
        raise LegalReviewError(
            "pathway scope mismatch", code="pathway_scope_mismatch"
        )
    if item.reviewed_cohort_key and rule.cohort_key != item.reviewed_cohort_key:
        raise LegalReviewError(
            "cohort scope mismatch", code="cohort_scope_mismatch"
        )
    if (
        item.reviewed_effective_from is not None
        and rule.effective_from is not None
        and item.reviewed_effective_from != rule.effective_from
    ):
        raise LegalReviewError(
            "effective period mismatch", code="effective_period_mismatch"
        )

    rule.review_status = "approved"
    rule.verification_status = "human_verified"
    rule.reviewed_by = event.actor_id
    rule.reviewed_at = event.occurred_at or _utcnow()
    note = (rule.verification_notes or "").strip()
    promo_note = f"promoted_via_review_item={item.id};event={event.id};{reason}"
    rule.verification_notes = (
        f"{note}; {promo_note}".strip("; ") if note else promo_note
    )
    session.add(rule)
    session.flush()

    # Post-condition: invariant must pass.
    check = validate_legal_rule_human_authority(session, rule)
    if not check.ok:
        raise LegalReviewError(
            f"promotion failed invariant: {','.join(check.reasons)}",
            code="promotion_invariant_failed",
        )
    return rule


def demote_legal_rule_authority(
    session: Session,
    *,
    legal_rule_id: int,
    reason: str,
) -> LegalRule | None:
    """Fail-closed demotion: clear human_verified when approval is no longer valid.

    Preserves historical review events. Safe to call repeatedly.
    """
    rule = session.execute(
        select(LegalRule).where(LegalRule.id == legal_rule_id).with_for_update()
    ).scalar_one_or_none()
    if rule is None:
        return None
    if rule.verification_status != "human_verified" and rule.review_status != "approved":
        return rule

    rule.verification_status = "potentially_outdated"
    if rule.review_status == "approved":
        rule.review_status = "stale_pending_review"
    note = (rule.verification_notes or "").strip()
    demote_note = f"demoted:{reason};at={_utcnow().isoformat()}"
    rule.verification_notes = (
        f"{note}; {demote_note}".strip("; ") if note else demote_note
    )
    # Retain reviewed_by/reviewed_at as historical audit of last promotion attempt.
    session.add(rule)
    session.flush()
    return rule


def demote_if_authority_invalid(
    session: Session, *, legal_rule_id: int, reason: str
) -> None:
    """Demote when invariant fails (used after invalidation events)."""
    rule = session.get(LegalRule, legal_rule_id)
    if rule is None:
        return
    if rule.verification_status != "human_verified":
        return
    check = validate_legal_rule_human_authority(session, rule)
    if not check.ok:
        demote_legal_rule_authority(
            session, legal_rule_id=legal_rule_id, reason=reason
        )
