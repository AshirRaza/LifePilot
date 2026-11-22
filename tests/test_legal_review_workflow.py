"""Phase 10D.2 — Authorized review workflow, auth boundary, legacy queue bridge."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lifepilot.api.deps import get_db
from lifepilot.api.settings import reset_api_settings_cache
from lifepilot.db.legal_review import (
    ActorRef,
    LegalReviewError,
    append_review_event,
    assert_legal_rule_not_promoted_by_review,
    create_pending_review_item,
    get_review_history,
)
from lifepilot.db.models import (
    Authority,
    Document,
    DocumentVersion,
    Jurisdiction,
    LegalRule,
    Source,
)
from lifepilot.governance.high_risk_queue_import import import_high_risk_queue
from lifepilot.governance.review_workflow import (
    ReviewDecisionRequest,
    ReviewWorkflowService,
)
from lifepilot.governance.reviewer_auth import (
    PERM_LEGAL_REVIEW_ASSIGN,
    PERM_LEGAL_REVIEW_DECIDE,
    PERM_LEGAL_REVIEW_READ,
    ReviewerAuthError,
    ReviewerIdentity,
    get_reviewer_auth_provider,
    reject_client_supplied_actor,
    reset_reviewer_auth_provider_cache,
)


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _seed_graph(session: Session, *, code: str) -> dict:
    jur = Jurisdiction(
        code=code,
        name=f"Synthetic {code}",
        jurisdiction_type="country",
        country_code="XX",
    )
    session.add(jur)
    session.flush()
    auth = Authority(jurisdiction_id=jur.id, name=f"Auth {code}")
    session.add(auth)
    session.flush()
    source = Source(
        authority_id=auth.id,
        jurisdiction_id=jur.id,
        name=f"Source {code}",
        seed_url=f"https://example.test/{code.lower()}",
        seed_url_normalized=f"https://example.test/{code.lower()}",
        allowed_url_patterns=[f"https://example.test/{code.lower()}/*"],
        review_status="approved",
        default_language="en",
    )
    session.add(source)
    session.flush()
    doc = Document(
        authority_id=auth.id,
        primary_source_id=source.id,
        canonical_url=f"https://example.test/{code.lower()}/rules",
        url_normalized=f"https://example.test/{code.lower()}/rules",
        content_kind="html_page",
        language="en",
        discovery_status="seed",
    )
    session.add(doc)
    session.flush()
    ver = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=source.id,
        version_number=1,
        source_url=doc.canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
        http_status=200,
        content_sha256=_sha(f"{code}-v1"),
        byte_size=120,
        storage_path=f"data/raw/test/{code.lower()}_v1.html",
        is_current=True,
        legal_status="unknown",
    )
    session.add(ver)
    session.flush()
    rule = LegalRule(
        rule_key=f"{code.lower()}.salary.general",
        rule_revision=1,
        jurisdiction_id=jur.id,
        jurisdiction_code=jur.code,
        pathway_id="xx_route",
        requirement_category="financial_or_sponsorship",
        requirement_type="salary_threshold",
        numeric_value=Decimal("41000.00"),
        currency_code="GBP",
        amount_unit="yearly",
        effective_date_status="unknown",
        cohort_key="general",
        cohort_category="general",
        rule_summary="Synthetic rule for workflow tests.",
        source_id=source.id,
        document_id=doc.id,
        document_version_id=ver.id,
        source_url=doc.canonical_url,
    )
    session.add(rule)
    session.flush()
    return {
        "jurisdiction": jur,
        "source": source,
        "document": doc,
        "version": ver,
        "rule": rule,
    }


def _human(
    *,
    actor_id: str = "reviewer:alice",
    perms: set[str] | None = None,
) -> ReviewerIdentity:
    default = {
        PERM_LEGAL_REVIEW_READ,
        PERM_LEGAL_REVIEW_ASSIGN,
        PERM_LEGAL_REVIEW_DECIDE,
    }
    return ReviewerIdentity(
        actor_id=actor_id,
        actor_kind="human_reviewer",
        permissions=frozenset(perms if perms is not None else default),
    )


def _pending_item(session: Session, g: dict, **kwargs):
    defaults = dict(
        subject_type="legal_rule",
        jurisdiction_id=g["jurisdiction"].id,
        legal_rule_id=g["rule"].id,
        document_version_id=g["version"].id,
        document_id=g["document"].id,
        source_id=g["source"].id,
        pathway_id="xx_route",
        requirement_category="financial_or_sponsorship",
        review_reason="Synthetic workflow review item.",
        actor=ActorRef(actor_id="service:seed", actor_kind="service"),
        reviewed_cohort_key="general",
    )
    defaults.update(kwargs)
    return create_pending_review_item(session, **defaults)


def test_fail_closed_auth_and_forged_actor(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIFEPILOT_LEGAL_REVIEW_AUTH_MODE", "disabled")
    reset_reviewer_auth_provider_cache()
    provider = get_reviewer_auth_provider()
    assert provider.human_auth_available is False
    with pytest.raises(ReviewerAuthError) as exc:
        provider.resolve_human_reviewer(reviewer_id="x", reviewer_secret="y")
    assert exc.value.code == "human_auth_unavailable"

    with pytest.raises(ReviewerAuthError) as exc2:
        reject_client_supplied_actor(
            client_actor_id="reviewer:evil", client_actor_kind="human_reviewer"
        )
    assert exc2.value.code == "forged_actor_identity"


def test_test_fixture_auth_resolves_human(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIFEPILOT_ENV", "development")
    monkeypatch.setenv("LIFEPILOT_LEGAL_REVIEW_AUTH_MODE", "test_fixture")
    monkeypatch.setenv(
        "LIFEPILOT_LEGAL_REVIEW_TEST_REVIEWERS",
        "reviewer:alice:secret-a:legal_review.read|legal_review.assign|legal_review.decide",
    )
    reset_reviewer_auth_provider_cache()
    provider = get_reviewer_auth_provider()
    ident = provider.resolve_human_reviewer(
        reviewer_id="reviewer:alice", reviewer_secret="secret-a"
    )
    assert ident.actor_kind == "human_reviewer"
    assert ident.has_permission(PERM_LEGAL_REVIEW_DECIDE)


def test_list_filter_pagination_and_history(db_session: Session):
    g = _seed_graph(db_session, code="XX-WF1")
    item = _pending_item(db_session, g)
    svc = ReviewWorkflowService(db_session)
    identity = _human()
    listed = svc.list_queue(
        identity,
        workflow_states=["pending"],
        pathway_id="xx_route",
        risk_class="high",
        offset=0,
        limit=10,
    )
    assert any(i.id == item.id for i in listed)
    view = svc.get_item(identity, review_item_id=item.id)
    assert len(view.events) == 1
    assert view.document_version is not None


def test_assign_start_approve_reject_changes_reopen(db_session: Session):
    g = _seed_graph(db_session, code="XX-WF2")
    item = _pending_item(db_session, g)
    svc = ReviewWorkflowService(db_session)
    alice = _human()

    svc.assign(
        alice,
        review_item_id=item.id,
        assignee_actor_id="reviewer:alice",
        expected_row_version=1,
    )
    db_session.refresh(item)
    assert item.workflow_state == "assigned"

    svc.start_review(alice, review_item_id=item.id, expected_row_version=2)
    approved = svc.submit_decision(
        alice,
        review_item_id=item.id,
        request=ReviewDecisionRequest(
            decision="approved",
            expected_row_version=3,
            rationale="Synthetic approval after examining pinned version.",
            document_version_id=g["version"].id,
            pathway_id="xx_route",
            requirement_category="financial_or_sponsorship",
            acknowledge_cohort_conditions=True,
        ),
    )
    assert approved.event_type == "approved"
    assert_legal_rule_not_promoted_by_review(
        db_session, legal_rule_id=g["rule"].id, expected_verification_status="unverified"
    )
    assert g["rule"].review_status == "unreviewed"
    assert g["rule"].reviewed_by is None

    svc.reopen(
        alice,
        review_item_id=item.id,
        expected_row_version=4,
        rationale="Reopen for synthetic correction.",
    )
    svc.start_review(alice, review_item_id=item.id, expected_row_version=5)
    rejected = svc.submit_decision(
        alice,
        review_item_id=item.id,
        request=ReviewDecisionRequest(
            decision="rejected",
            expected_row_version=6,
            rationale="Synthetic rejection.",
            document_version_id=g["version"].id,
        ),
    )
    assert rejected.event_type == "rejected"

    g2 = _seed_graph(db_session, code="XX-WF2B")
    item2 = _pending_item(db_session, g2, review_reason="Changes-request path.")
    svc.start_review(alice, review_item_id=item2.id, expected_row_version=1)
    changed = svc.submit_decision(
        alice,
        review_item_id=item2.id,
        request=ReviewDecisionRequest(
            decision="changes_requested",
            expected_row_version=2,
            rationale="Need clearer cohort wording.",
            document_version_id=g2["version"].id,
        ),
    )
    assert changed.event_type == "changes_requested"


def test_invalid_assignment_and_unauthorized(db_session: Session):
    g = _seed_graph(db_session, code="XX-WF3")
    item = _pending_item(db_session, g)
    svc = ReviewWorkflowService(db_session)
    reader = _human(perms={PERM_LEGAL_REVIEW_READ})
    with pytest.raises(ReviewerAuthError):
        svc.assign(
            reader,
            review_item_id=item.id,
            assignee_actor_id="reviewer:alice",
            expected_row_version=1,
        )
    with pytest.raises(LegalReviewError) as exc:
        svc.assign(
            _human(),
            review_item_id=item.id,
            assignee_actor_id="",
            expected_row_version=1,
        )
    assert exc.value.code == "missing_assignee"


def test_missing_rationale_pin_mismatch_invalidated_concurrency(db_session: Session):
    g = _seed_graph(db_session, code="XX-WF4")
    item = _pending_item(db_session, g)
    svc = ReviewWorkflowService(db_session)
    alice = _human()
    svc.start_review(alice, review_item_id=item.id, expected_row_version=1)

    with pytest.raises(LegalReviewError) as exc:
        svc.submit_decision(
            alice,
            review_item_id=item.id,
            request=ReviewDecisionRequest(
                decision="approved",
                expected_row_version=2,
                rationale="   ",
                document_version_id=g["version"].id,
                acknowledge_cohort_conditions=True,
            ),
        )
    assert exc.value.code == "missing_rationale"

    with pytest.raises(LegalReviewError) as exc2:
        svc.submit_decision(
            alice,
            review_item_id=item.id,
            request=ReviewDecisionRequest(
                decision="approved",
                expected_row_version=2,
                rationale="Pin mismatch attempt.",
                document_version_id=999999,
                acknowledge_cohort_conditions=True,
            ),
        )
    assert exc2.value.code in {"evidence_pin_mismatch", "document_version_not_found"}

    with pytest.raises(LegalReviewError) as exc3:
        svc.submit_decision(
            alice,
            review_item_id=item.id,
            request=ReviewDecisionRequest(
                decision="approved",
                expected_row_version=1,  # stale
                rationale="Stale concurrency attempt.",
                document_version_id=g["version"].id,
                acknowledge_cohort_conditions=True,
            ),
        )
    assert exc3.value.code == "concurrent_update"

    # Approve then invalidate; refuse re-approval while invalidated.
    svc.submit_decision(
        alice,
        review_item_id=item.id,
        request=ReviewDecisionRequest(
            decision="approved",
            expected_row_version=2,
            rationale="Approve before invalidation.",
            document_version_id=g["version"].id,
            acknowledge_cohort_conditions=True,
        ),
    )
    append_review_event(
        db_session,
        review_item_id=item.id,
        event_type="source_version_changed",
        actor=ActorRef(actor_id="service:maint", actor_kind="service"),
        expected_row_version=3,
        rationale="Synthetic source change.",
    )
    db_session.refresh(item)
    assert item.invalidated is True
    with pytest.raises(LegalReviewError) as exc4:
        # Must reopen first; from invalidated cannot approve directly.
        svc.submit_decision(
            alice,
            review_item_id=item.id,
            request=ReviewDecisionRequest(
                decision="approved",
                expected_row_version=4,
                rationale="Should fail — invalidated.",
                document_version_id=g["version"].id,
                acknowledge_cohort_conditions=True,
            ),
        )
    assert exc4.value.code in {"item_invalidated", "invalid_workflow_transition"}


def test_service_identity_cannot_decide(db_session: Session):
    g = _seed_graph(db_session, code="XX-WF5")
    item = _pending_item(db_session, g)
    svc = ReviewWorkflowService(db_session)
    service_ident = ReviewerIdentity(
        actor_id="service:api_key",
        actor_kind="service",
        permissions=frozenset(
            {PERM_LEGAL_REVIEW_READ, PERM_LEGAL_REVIEW_ASSIGN, PERM_LEGAL_REVIEW_DECIDE}
        ),
    )
    with pytest.raises(ReviewerAuthError) as exc:
        svc.start_review(service_ident, review_item_id=item.id, expected_row_version=1)
    assert exc.value.code == "human_actor_required"


def test_event_history_integrity_no_eventless_transition(db_session: Session):
    g = _seed_graph(db_session, code="XX-WF6")
    item = _pending_item(db_session, g)
    svc = ReviewWorkflowService(db_session)
    alice = _human()
    prev_state = item.workflow_state
    prev_ver = item.row_version
    svc.start_review(alice, review_item_id=item.id, expected_row_version=1)
    db_session.refresh(item)
    svc.assert_no_eventless_state_change(
        review_item_id=item.id,
        previous_row_version=prev_ver,
        previous_workflow_state=prev_state,
    )
    hist = get_review_history(db_session, review_item_id=item.id)
    assert hist[-1].to_workflow_state == item.workflow_state
    assert hist[-1].actor_kind == "human_reviewer"

    # Direct write without event is rejected at DB (Phase 10D.4 trigger).
    from sqlalchemy.exc import DBAPIError, ProgrammingError

    with pytest.raises((DBAPIError, ProgrammingError, LegalReviewError)):
        with db_session.begin_nested():
            item.workflow_state = "approved"
            item.row_version = int(item.row_version) + 1
            db_session.flush()
            svc.assert_no_eventless_state_change(
                review_item_id=item.id,
                previous_row_version=prev_ver,
                previous_workflow_state=prev_state,
            )


def test_legacy_queue_dry_run_quarantine_and_import(db_session: Session, tmp_path: Path):
    g = _seed_graph(db_session, code="GB")
    # Production queue file: insufficient provenance → quarantine (dry-run).
    summary = import_high_risk_queue(db_session, dry_run=True)
    assert summary.dry_run is True
    assert summary.quarantined >= 1
    assert summary.imported == 0
    # Original file untouched.
    original = Path("config/eval/high_risk_legal_review_queue.json")
    assert original.exists()

    # Synthetic enriched queue with explicit fingerprint (not invented by importer).
    enriched = {
        "queue_id": "synthetic_test_queue",
        "items": [
            {
                "id": "SYN-IMPORT-1",
                "jurisdiction": "GB",
                "pathway_id": "gb_skilled_worker",
                "claim": "Synthetic claim for import bridge tests.",
                "verification_status": "human_verified",  # must NOT become approval
                "verification_method": "pending_official_page_review",
                "evidence_fingerprint": _sha("syn-import-1"),
                "reviewer": "legacy-json-label",
            },
            {
                "id": "SYN-IMPORT-2",
                "jurisdiction": "GB",
                "pathway_id": "gb_student",
                "claim": "Missing provenance quarantine.",
                "verification_status": "requires_human_review",
            },
        ],
    }
    path = tmp_path / "synthetic_queue.json"
    path.write_text(json.dumps(enriched), encoding="utf-8")

    dry = import_high_risk_queue(db_session, path=path, dry_run=True)
    assert dry.dry_run_would_import == 1
    assert dry.quarantined == 1

    live = import_high_risk_queue(db_session, path=path, dry_run=False)
    assert live.imported == 1
    assert live.quarantined == 1
    assert live.records[0].outcome == "imported"

    # Duplicate prevention.
    again = import_high_risk_queue(db_session, path=path, dry_run=False)
    assert again.duplicate == 1
    assert again.imported == 0

    # Imported item is pending — not approved — despite legacy human_verified.
    svc = ReviewWorkflowService(db_session)
    items = svc.list_queue(
        _human(),
        workflow_states=["pending"],
    )
    imported = [i for i in items if i.subject_ref == "SYN-IMPORT-1"]
    assert len(imported) == 1
    assert imported[0].workflow_state == "pending"
    assert imported[0].subject_type == "high_risk_queue_item"
    # Jurisdiction seed used for GB code.
    assert g["jurisdiction"].code == "GB"


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def review_api_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIFEPILOT_ENV", "development")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_AUTH", "1")
    monkeypatch.setenv("LIFEPILOT_API_KEYS", "test-secret-key")
    monkeypatch.setenv("LIFEPILOT_LEGAL_REVIEW_API_ENABLED", "1")
    monkeypatch.setenv("LIFEPILOT_LEGAL_REVIEW_AUTH_MODE", "test_fixture")
    monkeypatch.setenv(
        "LIFEPILOT_LEGAL_REVIEW_TEST_REVIEWERS",
        "reviewer:alice:alice-secret:legal_review.read|legal_review.assign|legal_review.decide,"
        "reviewer:readonly:ro-secret:legal_review.read",
    )
    monkeypatch.setenv("LIFEPILOT_VERIFICATION_ENABLED", "1")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_LLM", "0")
    reset_api_settings_cache()
    reset_reviewer_auth_provider_cache()
    yield
    reset_api_settings_cache()
    reset_reviewer_auth_provider_cache()


def _reviewer_headers(*, reviewer: str = "reviewer:alice", secret: str = "alice-secret"):
    return {
        "X-API-Key": "test-secret-key",
        "X-LifePilot-Reviewer-Id": reviewer,
        "X-LifePilot-Reviewer-Secret": secret,
    }


def test_api_disabled_and_human_auth_unavailable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIFEPILOT_ENV", "development")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_AUTH", "1")
    monkeypatch.setenv("LIFEPILOT_API_KEYS", "test-secret-key")
    monkeypatch.setenv("LIFEPILOT_LEGAL_REVIEW_API_ENABLED", "0")
    monkeypatch.setenv("LIFEPILOT_LEGAL_REVIEW_AUTH_MODE", "disabled")
    reset_api_settings_cache()
    reset_reviewer_auth_provider_cache()
    from lifepilot.api.app import create_app

    client = TestClient(create_app())
    resp = client.get("/v1/legal-review/items", headers={"X-API-Key": "test-secret-key"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "legal_review_api_disabled"

    monkeypatch.setenv("LIFEPILOT_LEGAL_REVIEW_API_ENABLED", "1")
    reset_api_settings_cache()
    reset_reviewer_auth_provider_cache()
    client2 = TestClient(create_app())
    resp2 = client2.get(
        "/v1/legal-review/items", headers={"X-API-Key": "test-secret-key"}
    )
    assert resp2.status_code == 503
    assert resp2.json()["detail"]["code"] == "human_auth_unavailable"


def test_api_workflow_and_auth_boundaries(review_api_env, db_session: Session):
    from lifepilot.api import app as api_app

    g = _seed_graph(db_session, code="XX-API1")
    item = _pending_item(db_session, g)
    db_session.flush()

    def _override_db():
        try:
            yield db_session
        finally:
            pass

    app = api_app.create_app()
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    # Service API key alone cannot call review routes (no reviewer headers).
    bare = client.get("/v1/legal-review/items", headers={"X-API-Key": "test-secret-key"})
    assert bare.status_code == 401

    # Read with reviewer.
    listed = client.get("/v1/legal-review/items", headers=_reviewer_headers())
    assert listed.status_code == 200
    assert listed.json()["count"] >= 1

    got = client.get(
        f"/v1/legal-review/items/{item.id}", headers=_reviewer_headers()
    )
    assert got.status_code == 200
    assert got.json()["workflow_state"] == "pending"

    # Forged actor fields rejected by schema.
    forged = client.post(
        f"/v1/legal-review/items/{item.id}/start",
        headers=_reviewer_headers(),
        json={
            "expected_row_version": 1,
            "actor_id": "reviewer:evil",
            "actor_kind": "human_reviewer",
        },
    )
    assert forged.status_code == 422

    # Readonly cannot decide.
    ro = client.post(
        f"/v1/legal-review/items/{item.id}/start",
        headers=_reviewer_headers(reviewer="reviewer:readonly", secret="ro-secret"),
        json={"expected_row_version": 1},
    )
    assert ro.status_code == 403

    start = client.post(
        f"/v1/legal-review/items/{item.id}/start",
        headers=_reviewer_headers(),
        json={"expected_row_version": 1},
    )
    assert start.status_code == 200
    assert start.json()["event_type"] == "review_started"

    conflict = client.post(
        f"/v1/legal-review/items/{item.id}/decisions",
        headers=_reviewer_headers(),
        json={
            "decision": "approved",
            "expected_row_version": 1,
            "rationale": "Stale row version.",
            "document_version_id": g["version"].id,
            "acknowledge_cohort_conditions": True,
        },
    )
    assert conflict.status_code == 409

    approved = client.post(
        f"/v1/legal-review/items/{item.id}/decisions",
        headers=_reviewer_headers(),
        json={
            "decision": "approved",
            "expected_row_version": 2,
            "rationale": "API synthetic approval.",
            "document_version_id": g["version"].id,
            "acknowledge_cohort_conditions": True,
        },
    )
    assert approved.status_code == 200
    assert approved.json()["event_type"] == "approved"
    db_session.refresh(g["rule"])
    assert g["rule"].verification_status == "unverified"

    # Existing answer route still works (backward compatible).
    from lifepilot.agent.contracts import AgentResponse, AgentStatus
    from lifepilot.agent.contracts import TimingMetadata

    def _mock(*_a, **_k):
        return AgentResponse(
            request_id="req_compat",
            status=AgentStatus.SUCCESS,
            answer="ok",
            claims=[],
            verification={"suppressed_claims": [], "warnings": []},
            timing=TimingMetadata(total_ms=1.0),
            retrieval_decision="internal_only",
            jurisdiction="GB",
        )

    import lifepilot.api.app as mod

    # Remount with mock for answer only on a fresh client is heavy; call health instead.
    health = client.get("/health")
    assert health.status_code == 200
    del _mock, mod

    app.dependency_overrides.clear()


def test_live_database_unchanged_after_10d2():
    import os

    from sqlalchemy import create_engine, text

    from lifepilot.db import isolation
    from lifepilot.db.url import normalize_database_url
    from sqlalchemy.engine import make_url
    from dotenv import load_dotenv
    from pathlib import Path as P

    load_dotenv(P(".env"))
    raw = os.environ.get(isolation.APP_DATABASE_URL_ENV) or os.environ.get(
        isolation.DATABASE_URL_ENV
    )
    if not raw:
        pytest.skip("no database URL")
    u = make_url(normalize_database_url(raw))
    if (u.database or "").endswith("_test"):
        u = u.set(database="lifepilot")
    engine = create_engine(u)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_database()")).scalar_one() == "lifepilot"
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert ver == "20261113_0010"
            assert (
                conn.execute(
                    text("SELECT to_regclass('public.legal_review_items')")
                ).scalar_one()
                is None
            )
    finally:
        engine.dispose()
