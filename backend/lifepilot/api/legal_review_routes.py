"""Controlled legal review HTTP API (Phase 10D.2).

Decision routes require a configured human reviewer auth provider.
Service API keys never authorize human decisions.
Client-supplied actor_id / actor_kind are rejected.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from lifepilot.api.auth import AuthPrincipal
from lifepilot.api.deps import get_db
from lifepilot.api.schemas import (
    LegalReviewAssignRequest,
    LegalReviewDecisionRequest,
    LegalReviewEventResponse,
    LegalReviewItemResponse,
    LegalReviewListResponse,
    LegalReviewReopenRequest,
    LegalReviewStartRequest,
)
from lifepilot.api.settings import get_api_settings
from lifepilot.db.legal_review import LegalReviewError
from lifepilot.db.models import LegalReviewEvent, LegalReviewItem
from lifepilot.governance.review_workflow import (
    ReviewDecisionRequest,
    ReviewWorkflowService,
)
from lifepilot.governance.reviewer_auth import (
    ReviewerAuthError,
    ReviewerIdentity,
    get_reviewer_auth_provider,
)

router = APIRouter(prefix="/v1/legal-review", tags=["legal-review"])


def _ensure_api_enabled() -> None:
    cfg = get_api_settings()
    if not cfg.legal_review_api_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "legal_review_api_disabled",
                "message": (
                    "Legal review API is disabled. Set "
                    "LIFEPILOT_LEGAL_REVIEW_API_ENABLED=1 only with a trusted "
                    "reviewer auth provider configured."
                ),
            },
        )


def _resolve_reviewer(
    *,
    principal: str | None,
    reviewer_id: str | None,
    reviewer_secret: str | None,
) -> ReviewerIdentity:
    """Resolve human reviewer; service API keys alone are never sufficient."""
    del principal  # transport auth only — not a human reviewer
    provider = get_reviewer_auth_provider()
    if not provider.human_auth_available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "human_auth_unavailable",
                "message": (
                    "Human legal-review authentication is not configured. "
                    "Decision and reviewer routes remain disabled."
                ),
            },
        )
    try:
        return provider.resolve_human_reviewer(
            reviewer_id=reviewer_id,
            reviewer_secret=reviewer_secret,
        )
    except ReviewerAuthError as exc:
        status_code = status.HTTP_401_UNAUTHORIZED
        if exc.code == "permission_denied":
            status_code = status.HTTP_403_FORBIDDEN
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ReviewerAuthError):
        code = status.HTTP_403_FORBIDDEN
        if exc.code in {
            "human_auth_unavailable",
            "missing_reviewer_credentials",
            "invalid_reviewer_credentials",
        }:
            code = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.code == "human_auth_unavailable"
                else status.HTTP_401_UNAUTHORIZED
            )
        if exc.code == "forged_actor_identity":
            code = status.HTTP_400_BAD_REQUEST
        return HTTPException(
            status_code=code, detail={"code": exc.code, "message": str(exc)}
        )
    if isinstance(exc, LegalReviewError):
        code = status.HTTP_400_BAD_REQUEST
        if exc.code == "review_item_not_found":
            code = status.HTTP_404_NOT_FOUND
        elif exc.code == "concurrent_update":
            code = status.HTTP_409_CONFLICT
        return HTTPException(
            status_code=code, detail={"code": exc.code, "message": str(exc)}
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"code": "internal_error", "message": "unexpected error"},
    )


def _event_response(event: LegalReviewEvent) -> LegalReviewEventResponse:
    return LegalReviewEventResponse(
        id=event.id,
        event_type=event.event_type,
        from_workflow_state=event.from_workflow_state,
        to_workflow_state=event.to_workflow_state,
        occurred_at=event.occurred_at.isoformat(),
        actor_id=event.actor_id,
        actor_kind=event.actor_kind,
        rationale=event.rationale,
        document_version_id=event.document_version_id,
        evidence_fingerprint=event.evidence_fingerprint,
    )


def _item_response(
    item: LegalReviewItem, *, events: list[LegalReviewEvent] | None = None
) -> LegalReviewItemResponse:
    return LegalReviewItemResponse(
        id=item.id,
        public_id=str(item.public_id),
        subject_type=item.subject_type,
        subject_ref=item.subject_ref,
        legal_rule_id=item.legal_rule_id,
        jurisdiction_id=item.jurisdiction_id,
        pathway_id=item.pathway_id,
        requirement_category=item.requirement_category,
        risk_class=item.risk_class,
        workflow_state=item.workflow_state,
        review_reason=item.review_reason,
        priority=item.priority,
        document_version_id=item.document_version_id,
        evidence_fingerprint=item.evidence_fingerprint,
        assigned_actor_id=item.assigned_actor_id,
        assigned_actor_kind=item.assigned_actor_kind,
        invalidated=bool(item.invalidated),
        invalidation_reason=item.invalidation_reason,
        current_approved_event_id=item.current_approved_event_id,
        row_version=int(item.row_version),
        created_at=item.created_at.isoformat(),
        updated_at=item.updated_at.isoformat(),
        events=[_event_response(e) for e in events] if events is not None else None,
    )


ReviewerIdHeader = Annotated[
    str | None, Header(alias="X-LifePilot-Reviewer-Id")
]
ReviewerSecretHeader = Annotated[
    str | None, Header(alias="X-LifePilot-Reviewer-Secret")
]


@router.get("/items", response_model=LegalReviewListResponse)
def list_items(
    principal: AuthPrincipal,
    db: Annotated[Session, Depends(get_db)],
    reviewer_id: ReviewerIdHeader = None,
    reviewer_secret: ReviewerSecretHeader = None,
    workflow_state: Annotated[list[str] | None, Query()] = None,
    jurisdiction_id: int | None = None,
    pathway_id: str | None = None,
    risk_class: str | None = None,
    assigned_actor_id: str | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> LegalReviewListResponse:
    _ensure_api_enabled()
    identity = _resolve_reviewer(
        principal=principal, reviewer_id=reviewer_id, reviewer_secret=reviewer_secret
    )
    try:
        items = ReviewWorkflowService(db).list_queue(
            identity,
            workflow_states=workflow_state,
            jurisdiction_id=jurisdiction_id,
            pathway_id=pathway_id,
            risk_class=risk_class,
            assigned_actor_id=assigned_actor_id,
            offset=offset,
            limit=limit,
        )
    except (ReviewerAuthError, LegalReviewError) as exc:
        raise _map_error(exc) from exc
    return LegalReviewListResponse(
        items=[_item_response(i) for i in items],
        offset=offset,
        limit=limit,
        count=len(items),
    )


@router.get("/items/{review_item_id}", response_model=LegalReviewItemResponse)
def get_item(
    review_item_id: int,
    principal: AuthPrincipal,
    db: Annotated[Session, Depends(get_db)],
    reviewer_id: ReviewerIdHeader = None,
    reviewer_secret: ReviewerSecretHeader = None,
) -> LegalReviewItemResponse:
    _ensure_api_enabled()
    identity = _resolve_reviewer(
        principal=principal, reviewer_id=reviewer_id, reviewer_secret=reviewer_secret
    )
    try:
        view = ReviewWorkflowService(db).get_item(
            identity, review_item_id=review_item_id, include_history=True
        )
    except (ReviewerAuthError, LegalReviewError) as exc:
        raise _map_error(exc) from exc
    return _item_response(view.item, events=view.events)


@router.get(
    "/items/{review_item_id}/events",
    response_model=list[LegalReviewEventResponse],
)
def get_events(
    review_item_id: int,
    principal: AuthPrincipal,
    db: Annotated[Session, Depends(get_db)],
    reviewer_id: ReviewerIdHeader = None,
    reviewer_secret: ReviewerSecretHeader = None,
) -> list[LegalReviewEventResponse]:
    _ensure_api_enabled()
    identity = _resolve_reviewer(
        principal=principal, reviewer_id=reviewer_id, reviewer_secret=reviewer_secret
    )
    try:
        events = ReviewWorkflowService(db).get_history(
            identity, review_item_id=review_item_id
        )
    except (ReviewerAuthError, LegalReviewError) as exc:
        raise _map_error(exc) from exc
    return [_event_response(e) for e in events]


@router.post("/items/{review_item_id}/assign", response_model=LegalReviewEventResponse)
def assign_item(
    review_item_id: int,
    payload: LegalReviewAssignRequest,
    principal: AuthPrincipal,
    db: Annotated[Session, Depends(get_db)],
    reviewer_id: ReviewerIdHeader = None,
    reviewer_secret: ReviewerSecretHeader = None,
) -> LegalReviewEventResponse:
    _ensure_api_enabled()
    identity = _resolve_reviewer(
        principal=principal, reviewer_id=reviewer_id, reviewer_secret=reviewer_secret
    )
    try:
        event = ReviewWorkflowService(db).assign(
            identity,
            review_item_id=review_item_id,
            assignee_actor_id=payload.assignee_actor_id,
            expected_row_version=payload.expected_row_version,
            rationale=payload.rationale,
        )
    except (ReviewerAuthError, LegalReviewError) as exc:
        raise _map_error(exc) from exc
    return _event_response(event)


@router.post("/items/{review_item_id}/start", response_model=LegalReviewEventResponse)
def start_item(
    review_item_id: int,
    payload: LegalReviewStartRequest,
    principal: AuthPrincipal,
    db: Annotated[Session, Depends(get_db)],
    reviewer_id: ReviewerIdHeader = None,
    reviewer_secret: ReviewerSecretHeader = None,
) -> LegalReviewEventResponse:
    _ensure_api_enabled()
    identity = _resolve_reviewer(
        principal=principal, reviewer_id=reviewer_id, reviewer_secret=reviewer_secret
    )
    try:
        event = ReviewWorkflowService(db).start_review(
            identity,
            review_item_id=review_item_id,
            expected_row_version=payload.expected_row_version,
            rationale=payload.rationale,
        )
    except (ReviewerAuthError, LegalReviewError) as exc:
        raise _map_error(exc) from exc
    return _event_response(event)


@router.post("/items/{review_item_id}/decisions", response_model=LegalReviewEventResponse)
def submit_decision(
    review_item_id: int,
    payload: LegalReviewDecisionRequest,
    principal: AuthPrincipal,
    db: Annotated[Session, Depends(get_db)],
    reviewer_id: ReviewerIdHeader = None,
    reviewer_secret: ReviewerSecretHeader = None,
) -> LegalReviewEventResponse:
    _ensure_api_enabled()
    identity = _resolve_reviewer(
        principal=principal, reviewer_id=reviewer_id, reviewer_secret=reviewer_secret
    )
    try:
        event = ReviewWorkflowService(db).submit_decision(
            identity,
            review_item_id=review_item_id,
            request=ReviewDecisionRequest(
                decision=payload.decision,
                expected_row_version=payload.expected_row_version,
                rationale=payload.rationale,
                document_version_id=payload.document_version_id,
                evidence_fingerprint=payload.evidence_fingerprint,
                pathway_id=payload.pathway_id,
                requirement_category=payload.requirement_category,
                acknowledge_effective_period=payload.acknowledge_effective_period,
                acknowledge_cohort_conditions=payload.acknowledge_cohort_conditions,
            ),
        )
    except (ReviewerAuthError, LegalReviewError) as exc:
        raise _map_error(exc) from exc
    return _event_response(event)


@router.post("/items/{review_item_id}/reopen", response_model=LegalReviewEventResponse)
def reopen_item(
    review_item_id: int,
    payload: LegalReviewReopenRequest,
    principal: AuthPrincipal,
    db: Annotated[Session, Depends(get_db)],
    reviewer_id: ReviewerIdHeader = None,
    reviewer_secret: ReviewerSecretHeader = None,
) -> LegalReviewEventResponse:
    _ensure_api_enabled()
    identity = _resolve_reviewer(
        principal=principal, reviewer_id=reviewer_id, reviewer_secret=reviewer_secret
    )
    try:
        event = ReviewWorkflowService(db).reopen(
            identity,
            review_item_id=review_item_id,
            expected_row_version=payload.expected_row_version,
            rationale=payload.rationale,
        )
    except (ReviewerAuthError, LegalReviewError) as exc:
        raise _map_error(exc) from exc
    return _event_response(event)
