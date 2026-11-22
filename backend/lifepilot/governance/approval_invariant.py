"""Central LegalRule human-verification authority invariant (Phase 10D.4).

A LegalRule row flagged ``human_verified`` is not sufficient. Authority requires
a durable, non-invalidated approving review event pinned to matching evidence
and legal scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from lifepilot.db.models import LegalReviewEvent, LegalReviewItem, LegalRule


@dataclass(frozen=True)
class ApprovalValidationResult:
    ok: bool
    reasons: tuple[str, ...] = ()
    review_item_id: int | None = None
    approval_event_id: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "review_item_id": self.review_item_id,
            "approval_event_id": self.approval_event_id,
            "details": dict(self.details),
        }


def _review_tables_available(session: Session) -> bool:
    try:
        bind = session.get_bind()
        insp = inspect(bind)
        return bool(
            insp.has_table("legal_review_items") and insp.has_table("legal_review_events")
        )
    except Exception:  # noqa: BLE001
        return False


def validate_legal_rule_human_authority(
    session: Session,
    rule: LegalRule,
    *,
    check_pending_material: bool = True,
    require_review_linkage: bool = True,
) -> ApprovalValidationResult:
    """Deterministic check: may this LegalRule be treated as human-verified authority?

    Fail closed. Multiple conflicting approvals do not establish authority —
    exactly one non-invalidated approved item with a matching event is required.
    """
    reasons: list[str] = []

    if (rule.review_status or "") != "approved":
        reasons.append("review_status_not_approved")
    if (rule.verification_status or "") != "human_verified":
        reasons.append("verification_status_not_human_verified")
    if not (rule.reviewed_by or "").strip():
        reasons.append("missing_reviewed_by")
    if rule.reviewed_at is None:
        reasons.append("missing_reviewed_at")
    if rule.document_version_id is None:
        reasons.append("missing_document_version_pin")

    if reasons:
        return ApprovalValidationResult(ok=False, reasons=tuple(reasons))

    if not require_review_linkage:
        return ApprovalValidationResult(ok=True, reasons=())

    if not _review_tables_available(session):
        # Live schema without 0012: cannot prove linkage — fail closed for authority.
        return ApprovalValidationResult(
            ok=False,
            reasons=("legal_review_tables_unavailable",),
        )

    items = list(
        session.scalars(
            select(LegalReviewItem).where(LegalReviewItem.legal_rule_id == rule.id)
        ).all()
    )
    if not items:
        return ApprovalValidationResult(
            ok=False, reasons=("missing_linked_review_item",)
        )

    candidates: list[tuple[LegalReviewItem, LegalReviewEvent]] = []
    for item in items:
        if item.invalidated or item.workflow_state != "approved":
            continue
        if item.current_approved_event_id is None:
            continue
        event = session.get(LegalReviewEvent, item.current_approved_event_id)
        if event is None:
            continue
        if event.event_type != "approved":
            continue
        if event.actor_kind != "human_reviewer":
            continue
        if not (event.actor_id or "").strip():
            continue
        candidates.append((item, event))

    if not candidates:
        # Distinguish invalidated vs never approved.
        if any(
            i.invalidated or i.workflow_state == "invalidated" for i in items
        ):
            return ApprovalValidationResult(
                ok=False, reasons=("linked_review_item_invalidated",)
            )
        return ApprovalValidationResult(
            ok=False, reasons=("missing_approval_event",)
        )

    if len(candidates) > 1:
        # Conflicting active approvals — fail closed.
        return ApprovalValidationResult(
            ok=False,
            reasons=("conflicting_active_approvals",),
            details={"candidate_item_ids": [c[0].id for c in candidates]},
        )

    item, event = candidates[0]

    if item.document_version_id is None:
        return ApprovalValidationResult(
            ok=False,
            reasons=("review_item_missing_document_version_pin",),
            review_item_id=item.id,
            approval_event_id=event.id,
        )
    if int(item.document_version_id) != int(rule.document_version_id or 0):
        return ApprovalValidationResult(
            ok=False,
            reasons=("document_version_pin_mismatch",),
            review_item_id=item.id,
            approval_event_id=event.id,
            details={
                "rule_document_version_id": rule.document_version_id,
                "item_document_version_id": item.document_version_id,
            },
        )
    if (
        event.document_version_id is not None
        and int(event.document_version_id) != int(rule.document_version_id or 0)
    ):
        return ApprovalValidationResult(
            ok=False,
            reasons=("approval_event_version_mismatch",),
            review_item_id=item.id,
            approval_event_id=event.id,
        )

    # Evidence fingerprint when both sides carry one.
    if item.evidence_fingerprint and event.evidence_fingerprint:
        if item.evidence_fingerprint != event.evidence_fingerprint:
            return ApprovalValidationResult(
                ok=False,
                reasons=("evidence_fingerprint_mismatch",),
                review_item_id=item.id,
                approval_event_id=event.id,
            )

    # Scope: pathway / cohort / effective window when recorded on the item.
    if item.pathway_id and rule.pathway_id and item.pathway_id != rule.pathway_id:
        return ApprovalValidationResult(
            ok=False,
            reasons=("pathway_scope_mismatch",),
            review_item_id=item.id,
            approval_event_id=event.id,
        )
    if item.reviewed_cohort_key and rule.cohort_key:
        if item.reviewed_cohort_key != rule.cohort_key:
            return ApprovalValidationResult(
                ok=False,
                reasons=("cohort_scope_mismatch",),
                review_item_id=item.id,
                approval_event_id=event.id,
            )
    if item.reviewed_effective_from is not None and rule.effective_from is not None:
        if item.reviewed_effective_from != rule.effective_from:
            return ApprovalValidationResult(
                ok=False,
                reasons=("effective_period_mismatch",),
                review_item_id=item.id,
                approval_event_id=event.id,
            )
    if item.reviewed_effective_to is not None and rule.effective_to is not None:
        if item.reviewed_effective_to != rule.effective_to:
            return ApprovalValidationResult(
                ok=False,
                reasons=("effective_period_mismatch",),
                review_item_id=item.id,
                approval_event_id=event.id,
            )

    # reviewed_by should match approving actor (allow prefix-compatible equality).
    if (rule.reviewed_by or "").strip() != (event.actor_id or "").strip():
        return ApprovalValidationResult(
            ok=False,
            reasons=("reviewed_by_actor_mismatch",),
            review_item_id=item.id,
            approval_event_id=event.id,
        )

    # Optional rule-revision pin recorded on the approving event metadata.
    meta = event.event_metadata if isinstance(event.event_metadata, dict) else {}
    pinned_rev = meta.get("reviewed_rule_revision")
    if pinned_rev is not None:
        try:
            if int(pinned_rev) != int(rule.rule_revision or 0):
                return ApprovalValidationResult(
                    ok=False,
                    reasons=("rule_revision_mismatch",),
                    review_item_id=item.id,
                    approval_event_id=event.id,
                    details={
                        "rule_revision": rule.rule_revision,
                        "reviewed_rule_revision": pinned_rev,
                    },
                )
        except (TypeError, ValueError):
            return ApprovalValidationResult(
                ok=False,
                reasons=("rule_revision_mismatch",),
                review_item_id=item.id,
                approval_event_id=event.id,
            )

    if check_pending_material and rule.document_version_id is not None:
        try:
            from lifepilot.maintenance.pending_changes import load_pending_changes

            pin = int(rule.document_version_id)
            for pend in load_pending_changes().get("items") or []:
                # Require the pending change to supersede this rule's pinned version.
                # document_id alone is unsafe: test DB IDs restart while JSON persists.
                prev = pend.get("previous_version_id")
                try:
                    prev_i = int(prev) if prev is not None else None
                except (TypeError, ValueError):
                    prev_i = None
                if prev_i != pin:
                    continue
                status = str(pend.get("status") or "").lower()
                change_class = str(pend.get("change_class") or "").lower()
                if status in {"pending_review", "queued_for_review"} or change_class in {
                    "potentially_material",
                    "uncertain",
                    "confirmed_material",
                    "materiality_unknown",
                }:
                    return ApprovalValidationResult(
                        ok=False,
                        reasons=("unresolved_potentially_material_source_change",),
                        review_item_id=item.id,
                        approval_event_id=event.id,
                    )
        except Exception:  # noqa: BLE001
            pass

    return ApprovalValidationResult(
        ok=True,
        reasons=(),
        review_item_id=item.id,
        approval_event_id=event.id,
    )


def legal_rule_is_authoritative(session: Session, rule: LegalRule) -> bool:
    return validate_legal_rule_human_authority(session, rule).ok
