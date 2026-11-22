"""Authorized legal review workflow service (Phase 10D.2).

All state transitions go through ``append_review_event``. This service never
mutates ``LegalRule`` verification fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal

from sqlalchemy.orm import Session

from lifepilot.db.legal_review import (
    ActorRef,
    LegalReviewError,
    append_review_event,
    assert_legal_rule_not_promoted_by_review,
    get_review_history,
    get_review_item,
    list_review_items,
)
from lifepilot.db.models import DocumentVersion, LegalReviewEvent, LegalReviewItem, LegalRule
from lifepilot.governance.reviewer_auth import (
    PERM_LEGAL_REVIEW_ADMIN,
    PERM_LEGAL_REVIEW_ASSIGN,
    PERM_LEGAL_REVIEW_DECIDE,
    PERM_LEGAL_REVIEW_READ,
    ReviewerAuthError,
    ReviewerIdentity,
)

DecisionType = Literal["approved", "rejected", "changes_requested"]


@dataclass(frozen=True)
class ReviewDecisionRequest:
    """Validated human decision payload (identity comes from auth, not body)."""

    decision: DecisionType
    expected_row_version: int
    rationale: str
    document_version_id: int | None = None
    evidence_fingerprint: str | None = None
    pathway_id: str | None = None
    requirement_category: str | None = None
    acknowledge_effective_period: bool = False
    acknowledge_cohort_conditions: bool = False
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReviewItemView:
    item: LegalReviewItem
    events: list[LegalReviewEvent]
    document_version: DocumentVersion | None
    legal_rule: LegalRule | None


class ReviewWorkflowService:
    """Single writable state machine for interactive legal review."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_queue(
        self,
        identity: ReviewerIdentity,
        *,
        workflow_states: Iterable[str] | None = None,
        jurisdiction_id: int | None = None,
        pathway_id: str | None = None,
        risk_class: str | None = None,
        assigned_actor_id: str | None = None,
        legal_rule_id: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[LegalReviewItem]:
        identity.require_permission(PERM_LEGAL_REVIEW_READ)
        return list_review_items(
            self._session,
            workflow_states=workflow_states,
            jurisdiction_id=jurisdiction_id,
            pathway_id=pathway_id,
            risk_class=risk_class,
            assigned_actor_id=assigned_actor_id,
            legal_rule_id=legal_rule_id,
            offset=offset,
            limit=limit,
        )

    def get_item(
        self,
        identity: ReviewerIdentity,
        *,
        review_item_id: int | None = None,
        public_id: str | None = None,
        include_history: bool = True,
    ) -> ReviewItemView:
        identity.require_permission(PERM_LEGAL_REVIEW_READ)
        item = get_review_item(
            self._session,
            review_item_id=review_item_id,
            public_id=public_id,
            with_events=include_history,
        )
        if item is None:
            raise LegalReviewError("review item not found", code="review_item_not_found")
        events = (
            list(item.events)
            if include_history and item.events is not None
            else get_review_history(self._session, review_item_id=item.id)
        )
        version = (
            self._session.get(DocumentVersion, item.document_version_id)
            if item.document_version_id is not None
            else None
        )
        rule = (
            self._session.get(LegalRule, item.legal_rule_id)
            if item.legal_rule_id is not None
            else None
        )
        return ReviewItemView(
            item=item, events=events, document_version=version, legal_rule=rule
        )

    def get_history(
        self,
        identity: ReviewerIdentity,
        *,
        review_item_id: int,
    ) -> list[LegalReviewEvent]:
        identity.require_permission(PERM_LEGAL_REVIEW_READ)
        item = get_review_item(self._session, review_item_id=review_item_id)
        if item is None:
            raise LegalReviewError("review item not found", code="review_item_not_found")
        return get_review_history(self._session, review_item_id=review_item_id)

    # ------------------------------------------------------------------
    # Mutations (always via append_review_event)
    # ------------------------------------------------------------------

    def assign(
        self,
        identity: ReviewerIdentity,
        *,
        review_item_id: int,
        assignee_actor_id: str,
        expected_row_version: int,
        rationale: str | None = None,
    ) -> LegalReviewEvent:
        identity.require_permission(PERM_LEGAL_REVIEW_ASSIGN)
        if identity.actor_kind != "human_reviewer" and not identity.has_permission(
            PERM_LEGAL_REVIEW_ADMIN
        ):
            # Assign still requires a trusted human (or admin human) principal.
            raise ReviewerAuthError(
                "only authenticated human reviewers may assign",
                code="human_actor_required",
            )
        assignee_id = (assignee_actor_id or "").strip()
        if not assignee_id:
            raise LegalReviewError("assignee_actor_id required", code="missing_assignee")

        item = get_review_item(self._session, review_item_id=review_item_id)
        if item is None:
            raise LegalReviewError("review item not found", code="review_item_not_found")

        # Idempotent: already assigned to the same human in assigned state.
        if (
            item.workflow_state == "assigned"
            and item.assigned_actor_id == assignee_id
            and int(item.row_version) == int(expected_row_version)
        ):
            hist = get_review_history(self._session, review_item_id=item.id)
            if hist and hist[-1].event_type == "assigned":
                return hist[-1]

        assignee = ActorRef(actor_id=assignee_id, actor_kind="human_reviewer")
        return append_review_event(
            self._session,
            review_item_id=review_item_id,
            event_type="assigned",
            actor=identity.as_actor(),
            expected_row_version=expected_row_version,
            rationale=rationale or f"Assigned to {assignee_id}",
            assigned_to=assignee,
            event_metadata={"operation": "assign"},
        )

    def start_review(
        self,
        identity: ReviewerIdentity,
        *,
        review_item_id: int,
        expected_row_version: int,
        rationale: str | None = None,
    ) -> LegalReviewEvent:
        identity.require_permission(PERM_LEGAL_REVIEW_DECIDE)
        self._require_human(identity)
        item = get_review_item(self._session, review_item_id=review_item_id)
        if item is None:
            raise LegalReviewError("review item not found", code="review_item_not_found")

        # Idempotent: already in_review by this actor.
        if (
            item.workflow_state == "in_review"
            and item.assigned_actor_id == identity.actor_id
            and int(item.row_version) == int(expected_row_version)
        ):
            hist = get_review_history(self._session, review_item_id=item.id)
            if hist and hist[-1].event_type == "review_started":
                return hist[-1]

        return append_review_event(
            self._session,
            review_item_id=review_item_id,
            event_type="review_started",
            actor=identity.as_actor(),
            expected_row_version=expected_row_version,
            rationale=rationale or "Review started",
            event_metadata={"operation": "start_review"},
        )

    def submit_decision(
        self,
        identity: ReviewerIdentity,
        *,
        review_item_id: int,
        request: ReviewDecisionRequest,
    ) -> LegalReviewEvent:
        identity.require_permission(PERM_LEGAL_REVIEW_DECIDE)
        self._require_human(identity)
        if request.decision not in {"approved", "rejected", "changes_requested"}:
            raise LegalReviewError(
                f"invalid decision={request.decision!r}", code="invalid_decision"
            )

        item = get_review_item(self._session, review_item_id=review_item_id)
        if item is None:
            raise LegalReviewError("review item not found", code="review_item_not_found")

        self._validate_decision(item, request)

        before_verification = None
        before_review_status = None
        before_reviewed_by = None
        before_reviewed_at = None
        if item.legal_rule_id is not None:
            rule_before = self._session.get(LegalRule, item.legal_rule_id)
            if rule_before is not None:
                before_verification = rule_before.verification_status
                before_review_status = rule_before.review_status
                before_reviewed_by = rule_before.reviewed_by
                before_reviewed_at = rule_before.reviewed_at

        decision_meta: dict[str, Any] = {
            "operation": "submit_decision",
            "pathway_id": request.pathway_id,
            "requirement_category": request.requirement_category,
            "acknowledge_effective_period": request.acknowledge_effective_period,
            "acknowledge_cohort_conditions": request.acknowledge_cohort_conditions,
            **dict(request.metadata or {}),
        }
        if item.legal_rule_id is not None:
            linked_rule = self._session.get(LegalRule, item.legal_rule_id)
            if linked_rule is not None and request.decision == "approved":
                decision_meta["reviewed_rule_revision"] = int(
                    linked_rule.rule_revision or 1
                )

        event = append_review_event(
            self._session,
            review_item_id=review_item_id,
            event_type=request.decision,
            actor=identity.as_actor(),
            expected_row_version=request.expected_row_version,
            rationale=request.rationale.strip(),
            document_version_id=request.document_version_id,
            evidence_fingerprint=request.evidence_fingerprint,
            event_metadata=decision_meta,
        )

        # Hard invariant for 10D.2: never promote or mutate LegalRule verification.
        if item.legal_rule_id is not None and before_verification is not None:
            assert_legal_rule_not_promoted_by_review(
                self._session,
                legal_rule_id=item.legal_rule_id,
                expected_verification_status=before_verification,
            )
            rule_after = self._session.get(LegalRule, item.legal_rule_id)
            if rule_after is None:
                raise LegalReviewError(
                    "legal_rule missing after decision", code="legal_rule_not_found"
                )
            if (
                rule_after.review_status != before_review_status
                or rule_after.reviewed_by != before_reviewed_by
                or rule_after.reviewed_at != before_reviewed_at
            ):
                raise LegalReviewError(
                    "LegalRule review audit fields changed unexpectedly",
                    code="unexpected_rule_promotion",
                )
        return event

    def reopen(
        self,
        identity: ReviewerIdentity,
        *,
        review_item_id: int,
        expected_row_version: int,
        rationale: str,
    ) -> LegalReviewEvent:
        if not (
            identity.has_permission(PERM_LEGAL_REVIEW_ADMIN)
            or identity.has_permission(PERM_LEGAL_REVIEW_DECIDE)
        ):
            raise ReviewerAuthError(
                "missing permission to reopen", code="permission_denied"
            )
        self._require_human(identity)
        reason = (rationale or "").strip()
        if not reason:
            raise LegalReviewError("rationale required to reopen", code="missing_rationale")
        return append_review_event(
            self._session,
            review_item_id=review_item_id,
            event_type="reopened",
            actor=identity.as_actor(),
            expected_row_version=expected_row_version,
            rationale=reason,
            event_metadata={"operation": "reopen"},
        )

    # ------------------------------------------------------------------
    # Integrity helpers
    # ------------------------------------------------------------------

    def assert_no_eventless_state_change(
        self,
        *,
        review_item_id: int,
        previous_row_version: int,
        previous_workflow_state: str,
    ) -> None:
        """Regression helper: row_version/state changes require a new event."""
        item = get_review_item(self._session, review_item_id=review_item_id)
        if item is None:
            raise LegalReviewError("review item not found", code="review_item_not_found")
        if (
            int(item.row_version) != int(previous_row_version)
            or item.workflow_state != previous_workflow_state
        ):
            hist = get_review_history(self._session, review_item_id=review_item_id)
            if not hist:
                raise LegalReviewError(
                    "state changed without events", code="eventless_transition"
                )
            last = hist[-1]
            if last.to_workflow_state != item.workflow_state:
                raise LegalReviewError(
                    "item state does not match latest event",
                    code="eventless_transition",
                )

    @staticmethod
    def _require_human(identity: ReviewerIdentity) -> None:
        if identity.actor_kind != "human_reviewer":
            raise ReviewerAuthError(
                "human reviewer required",
                code="human_actor_required",
            )

    def _validate_decision(
        self, item: LegalReviewItem, request: ReviewDecisionRequest
    ) -> None:
        rationale = (request.rationale or "").strip()
        if not rationale:
            raise LegalReviewError("rationale is required", code="missing_rationale")

        if item.invalidated and request.decision == "approved":
            raise LegalReviewError(
                "cannot approve an invalidated review item",
                code="item_invalidated",
            )

        # Evidence pin must match the examined item pin.
        if request.document_version_id is not None:
            if (
                item.document_version_id is not None
                and int(request.document_version_id) != int(item.document_version_id)
            ):
                raise LegalReviewError(
                    "document_version_id does not match review item pin",
                    code="evidence_pin_mismatch",
                )
            ver = self._session.get(DocumentVersion, request.document_version_id)
            if ver is None:
                raise LegalReviewError(
                    "document_version_id not found",
                    code="document_version_not_found",
                )
        elif item.document_version_id is not None and request.decision == "approved":
            raise LegalReviewError(
                "approval requires confirming document_version_id pin",
                code="missing_evidence_pin_confirmation",
            )

        if request.evidence_fingerprint is not None:
            fp = request.evidence_fingerprint.strip()
            if item.evidence_fingerprint and fp != item.evidence_fingerprint:
                raise LegalReviewError(
                    "evidence_fingerprint does not match review item pin",
                    code="evidence_pin_mismatch",
                )
        elif item.evidence_fingerprint and request.decision == "approved":
            # Fingerprint-only items still require confirmation on approve.
            if item.document_version_id is None:
                raise LegalReviewError(
                    "approval requires confirming evidence_fingerprint pin",
                    code="missing_evidence_pin_confirmation",
                )

        if request.pathway_id is not None and item.pathway_id:
            if request.pathway_id.strip() != item.pathway_id:
                raise LegalReviewError(
                    "pathway_id does not match review item scope",
                    code="pathway_scope_mismatch",
                )
        if request.requirement_category is not None and item.requirement_category:
            if request.requirement_category.strip() != item.requirement_category:
                raise LegalReviewError(
                    "requirement_category does not match review item scope",
                    code="requirement_scope_mismatch",
                )

        if request.decision == "approved":
            needs_period = (
                item.reviewed_effective_from is not None
                or item.reviewed_effective_to is not None
            )
            if needs_period and not request.acknowledge_effective_period:
                raise LegalReviewError(
                    "acknowledge_effective_period required for approval",
                    code="missing_effective_period_ack",
                )
            if item.reviewed_cohort_key and not request.acknowledge_cohort_conditions:
                raise LegalReviewError(
                    "acknowledge_cohort_conditions required for approval",
                    code="missing_cohort_ack",
                )
