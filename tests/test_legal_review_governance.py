"""Phase 10D.1 — Persistent legal review governance data model tests.

Synthetic fixtures only. Live ``lifepilot`` is never modified.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from lifepilot.db import isolation
from lifepilot.db.legal_review import (
    ActorRef,
    LegalReviewError,
    append_review_event,
    assert_legal_rule_not_promoted_by_review,
    create_pending_review_item,
    get_review_history,
    get_review_item,
    list_review_items,
)
from lifepilot.db.models import (
    Authority,
    Document,
    DocumentVersion,
    Jurisdiction,
    LegalReviewItem,
    LegalRule,
    Source,
)
from lifepilot.db.session import get_engine


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _seed_graph(session: Session, *, code: str = "XX-REV") -> dict:
    jur = Jurisdiction(
        code=code,
        name=f"Synthetic {code}",
        jurisdiction_type="country",
        country_code="XX",
    )
    session.add(jur)
    session.flush()
    auth = Authority(jurisdiction_id=jur.id, name=f"Synthetic Authority {code}")
    session.add(auth)
    session.flush()
    source = Source(
        authority_id=auth.id,
        jurisdiction_id=jur.id,
        name=f"Synthetic Source {code}",
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
        rule_summary="Synthetic unreviewed salary threshold for governance tests.",
        source_id=source.id,
        document_id=doc.id,
        document_version_id=ver.id,
        source_url=doc.canonical_url,
    )
    session.add(rule)
    session.flush()
    return {
        "jurisdiction": jur,
        "authority": auth,
        "source": source,
        "document": doc,
        "version": ver,
        "rule": rule,
    }


def _service_actor() -> ActorRef:
    return ActorRef(actor_id="service:maintenance_pipeline", actor_kind="service")


def _human_actor(aid: str = "reviewer:alice") -> ActorRef:
    return ActorRef(actor_id=aid, actor_kind="human_reviewer")


# ---------------------------------------------------------------------------
# Defaults, pinning, creation
# ---------------------------------------------------------------------------


def test_default_pending_state_and_legal_rule_linkage(db_session: Session):
    g = _seed_graph(db_session, code="XX-REV1")
    item = create_pending_review_item(
        db_session,
        subject_type="legal_rule",
        jurisdiction_id=g["jurisdiction"].id,
        legal_rule_id=g["rule"].id,
        document_version_id=g["version"].id,
        document_id=g["document"].id,
        source_id=g["source"].id,
        pathway_id="xx_route",
        requirement_category="financial_or_sponsorship",
        review_reason="Synthetic high-risk salary threshold requires human review.",
        actor=_service_actor(),
        reviewed_cohort_key="general",
    )
    db_session.flush()
    assert item.workflow_state == "pending"
    assert item.invalidated is False
    assert item.legal_rule_id == g["rule"].id
    assert item.document_version_id == g["version"].id
    assert item.risk_class == "high"
    hist = get_review_history(db_session, review_item_id=item.id)
    assert len(hist) == 1
    assert hist[0].event_type == "submitted_for_review"
    assert hist[0].actor_kind == "service"
    # Fail-closed: LegalRule remains unverified.
    assert_legal_rule_not_promoted_by_review(
        db_session, legal_rule_id=g["rule"].id, expected_verification_status="unverified"
    )
    assert g["rule"].review_status == "unreviewed"


def test_missing_source_provenance_rejected(db_session: Session):
    g = _seed_graph(db_session, code="XX-REV2")
    with pytest.raises(LegalReviewError) as exc:
        create_pending_review_item(
            db_session,
            subject_type="legal_rule",
            jurisdiction_id=g["jurisdiction"].id,
            legal_rule_id=g["rule"].id,
            document_version_id=None,
            review_reason="Missing pin.",
            actor=_service_actor(),
        )
    assert exc.value.code == "missing_document_version_pin"

    with pytest.raises(LegalReviewError) as exc2:
        create_pending_review_item(
            db_session,
            subject_type="claim_evidence",
            jurisdiction_id=g["jurisdiction"].id,
            subject_ref="claim:synthetic-1",
            document_version_id=None,
            evidence_fingerprint=None,
            review_reason="No pin at all.",
            actor=_service_actor(),
        )
    assert exc2.value.code == "missing_evidence_pin"


def test_document_version_mismatch_and_fk(db_session: Session):
    g1 = _seed_graph(db_session, code="XX-REV3A")
    g2 = _seed_graph(db_session, code="XX-REV3B")
    with pytest.raises(LegalReviewError) as exc:
        create_pending_review_item(
            db_session,
            subject_type="legal_rule",
            jurisdiction_id=g1["jurisdiction"].id,
            legal_rule_id=g1["rule"].id,
            document_version_id=g2["version"].id,
            review_reason="Wrong version pin.",
            actor=_service_actor(),
        )
    assert exc.value.code == "document_version_mismatch"


def test_claim_evidence_subject_with_fingerprint(db_session: Session):
    g = _seed_graph(db_session, code="XX-REV4")
    item = create_pending_review_item(
        db_session,
        subject_type="claim_evidence",
        jurisdiction_id=g["jurisdiction"].id,
        subject_ref="ev_synthetic_fingerprint_target",
        evidence_fingerprint=_sha("claim-evidence-pin"),
        review_reason="Claim-level review bridge item.",
        actor=_service_actor(),
        risk_class="medium",
        priority="p2",
    )
    assert item.workflow_state == "pending"
    assert item.legal_rule_id is None
    assert item.document_version_id is None
    assert item.evidence_fingerprint


# ---------------------------------------------------------------------------
# Events, immutability, transitions, actors
# ---------------------------------------------------------------------------


def test_event_append_history_and_immutability(db_session: Session):
    g = _seed_graph(db_session, code="XX-REV5")
    item = create_pending_review_item(
        db_session,
        subject_type="legal_rule",
        jurisdiction_id=g["jurisdiction"].id,
        legal_rule_id=g["rule"].id,
        document_version_id=g["version"].id,
        review_reason="Need human decision.",
        actor=_service_actor(),
    )
    e1 = append_review_event(
        db_session,
        review_item_id=item.id,
        event_type="assigned",
        actor=_human_actor(),
        expected_row_version=1,
        rationale="Assign to Alice.",
    )
    assert e1.to_workflow_state == "assigned"
    item2 = get_review_item(db_session, review_item_id=item.id)
    assert item2 is not None
    assert item2.workflow_state == "assigned"
    assert item2.row_version == 2
    assert item2.assigned_actor_id == "reviewer:alice"

    e2 = append_review_event(
        db_session,
        review_item_id=item.id,
        event_type="review_started",
        actor=_human_actor(),
        expected_row_version=2,
    )
    assert e2.to_workflow_state == "in_review"

    hist = get_review_history(db_session, review_item_id=item.id)
    assert [h.event_type for h in hist] == [
        "submitted_for_review",
        "assigned",
        "review_started",
    ]

    # UPDATE blocked by trigger.
    with pytest.raises(DBAPIError):
        with db_session.begin_nested():
            db_session.execute(
                text(
                    "UPDATE legal_review_events SET rationale = 'tamper' "
                    "WHERE id = :id"
                ),
                {"id": e1.id},
            )
            db_session.flush()

    # DELETE blocked by trigger.
    with pytest.raises(DBAPIError):
        with db_session.begin_nested():
            db_session.execute(
                text("DELETE FROM legal_review_events WHERE id = :id"),
                {"id": e1.id},
            )
            db_session.flush()


def test_invalid_workflow_transition_and_missing_actor(db_session: Session):
    g = _seed_graph(db_session, code="XX-REV6")
    item = create_pending_review_item(
        db_session,
        subject_type="legal_rule",
        jurisdiction_id=g["jurisdiction"].id,
        legal_rule_id=g["rule"].id,
        document_version_id=g["version"].id,
        review_reason="Transition checks.",
        actor=_service_actor(),
    )
    with pytest.raises(LegalReviewError) as exc:
        append_review_event(
            db_session,
            review_item_id=item.id,
            event_type="approved",
            actor=_human_actor(),
            expected_row_version=1,
        )
    assert exc.value.code == "invalid_workflow_transition"

    with pytest.raises(LegalReviewError) as exc2:
        ActorRef(actor_id="  ", actor_kind="human_reviewer")
    assert exc2.value.code == "missing_actor_id"

    # Service may start review, but cannot approve (human decision required).
    append_review_event(
        db_session,
        review_item_id=item.id,
        event_type="review_started",
        actor=_service_actor(),
        expected_row_version=1,
    )
    with pytest.raises(LegalReviewError) as exc3:
        append_review_event(
            db_session,
            review_item_id=item.id,
            event_type="approved",
            actor=_service_actor(),
            expected_row_version=2,
        )
    assert exc3.value.code == "human_actor_required"


def test_service_versus_human_actor_and_approval_non_promotion(db_session: Session):
    g = _seed_graph(db_session, code="XX-REV7")
    item = create_pending_review_item(
        db_session,
        subject_type="legal_rule",
        jurisdiction_id=g["jurisdiction"].id,
        legal_rule_id=g["rule"].id,
        document_version_id=g["version"].id,
        review_reason="Approval must not promote LegalRule yet.",
        actor=_service_actor(),
    )
    append_review_event(
        db_session,
        review_item_id=item.id,
        event_type="review_started",
        actor=_human_actor(),
        expected_row_version=1,
    )
    approved = append_review_event(
        db_session,
        review_item_id=item.id,
        event_type="approved",
        actor=_human_actor(),
        expected_row_version=2,
        rationale="Synthetic approval recorded on review item only.",
    )
    refreshed = get_review_item(db_session, review_item_id=item.id)
    assert refreshed is not None
    assert refreshed.workflow_state == "approved"
    assert refreshed.current_approved_event_id == approved.id
    # Critical invariant for 10D.1:
    assert_legal_rule_not_promoted_by_review(
        db_session, legal_rule_id=g["rule"].id, expected_verification_status="unverified"
    )
    assert g["rule"].review_status == "unreviewed"


def test_concurrent_update_safety(db_session: Session):
    g = _seed_graph(db_session, code="XX-REV8")
    item = create_pending_review_item(
        db_session,
        subject_type="legal_rule",
        jurisdiction_id=g["jurisdiction"].id,
        legal_rule_id=g["rule"].id,
        document_version_id=g["version"].id,
        review_reason="Concurrency.",
        actor=_service_actor(),
    )
    append_review_event(
        db_session,
        review_item_id=item.id,
        event_type="assigned",
        actor=_human_actor(),
        expected_row_version=1,
    )
    with pytest.raises(LegalReviewError) as exc:
        append_review_event(
            db_session,
            review_item_id=item.id,
            event_type="review_started",
            actor=_human_actor(),
            expected_row_version=1,  # stale
        )
    assert exc.value.code == "concurrent_update"


def test_superseded_source_version_behavior(db_session: Session):
    g = _seed_graph(db_session, code="XX-REV9")
    item = create_pending_review_item(
        db_session,
        subject_type="legal_rule",
        jurisdiction_id=g["jurisdiction"].id,
        legal_rule_id=g["rule"].id,
        document_version_id=g["version"].id,
        review_reason="Will be invalidated when source changes.",
        actor=_service_actor(),
    )
    append_review_event(
        db_session,
        review_item_id=item.id,
        event_type="review_started",
        actor=_human_actor(),
        expected_row_version=1,
    )
    append_review_event(
        db_session,
        review_item_id=item.id,
        event_type="approved",
        actor=_human_actor(),
        expected_row_version=2,
    )
    # New version created — review item does not auto-inherit; mark invalid.
    ver2 = DocumentVersion(
        document_id=g["document"].id,
        fetched_via_source_id=g["source"].id,
        version_number=2,
        source_url=g["document"].canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
        http_status=200,
        content_sha256=_sha("XX-REV9-v2"),
        byte_size=140,
        storage_path="data/raw/test/xx-rev9_v2.html",
        is_current=True,
        legal_status="unknown",
    )
    g["version"].is_current = False
    session = db_session
    session.add(ver2)
    session.flush()

    append_review_event(
        session,
        review_item_id=item.id,
        event_type="source_version_changed",
        actor=_service_actor(),
        expected_row_version=3,
        rationale="New DocumentVersion observed; prior approval not inherited.",
        document_version_id=ver2.id,
    )
    refreshed = get_review_item(session, review_item_id=item.id)
    assert refreshed is not None
    assert refreshed.workflow_state == "invalidated"
    assert refreshed.invalidated is True
    assert refreshed.current_approved_event_id is None
    assert_legal_rule_not_promoted_by_review(
        session, legal_rule_id=g["rule"].id, expected_verification_status="unverified"
    )


def test_existing_legal_rule_audit_constraints_intact(db_session: Session):
    g = _seed_graph(db_session, code="XX-REV10")
    # Still cannot mark human_verified without audit fields / linked approval event.
    from sqlalchemy.exc import DBAPIError, ProgrammingError

    with pytest.raises((IntegrityError, DBAPIError, ProgrammingError)):
        with db_session.begin_nested():
            g["rule"].verification_status = "human_verified"
            g["rule"].review_status = "approved"
            db_session.flush()


def test_list_and_get_by_public_id(db_session: Session):
    g = _seed_graph(db_session, code="XX-REV11")
    item = create_pending_review_item(
        db_session,
        subject_type="legal_rule",
        jurisdiction_id=g["jurisdiction"].id,
        legal_rule_id=g["rule"].id,
        document_version_id=g["version"].id,
        review_reason="List/get.",
        actor=_service_actor(),
    )
    found = get_review_item(db_session, public_id=item.public_id, with_events=True)
    assert found is not None
    assert found.id == item.id
    assert len(found.events) == 1
    listed = list_review_items(
        db_session, workflow_states=["pending"], jurisdiction_id=g["jurisdiction"].id
    )
    assert any(i.id == item.id for i in listed)


# ---------------------------------------------------------------------------
# Migration upgrade/downgrade (test DB only)
# ---------------------------------------------------------------------------


def test_migration_0012_upgrade_downgrade_preserves_corpus(test_database_url: str):
    import os

    isolation.assert_safe_test_database(
        test_database_url, os.environ.get(isolation.APP_DATABASE_URL_ENV)
    )
    engine = get_engine()
    with engine.connect() as conn:
        isolation.assert_connected_to_test_database(conn)

    cfg = Config(str(isolation.REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(isolation.REPO_ROOT / "alembic"))

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with SessionLocal() as session:
        isolation.clear_registry_tables(session)
        g = _seed_graph(session, code="XX-MIG12")
        chunk_count_before = session.execute(
            text("SELECT count(*) FROM document_chunks")
        ).scalar_one()
        rule_id = g["rule"].id
        ver_id = g["version"].id
        session.commit()

    # Ensure head includes 0012.
    engine.dispose()
    get_engine.cache_clear()
    command.upgrade(cfg, "head")
    engine = get_engine()
    with engine.connect() as conn:
        isolation.assert_connected_to_test_database(conn)
        tables = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname='public' AND tablename LIKE 'legal_review_%'"
                )
            )
        }
        assert "legal_review_items" in tables
        assert "legal_review_events" in tables

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with SessionLocal() as session:
        rule = session.get(LegalRule, rule_id)
        assert rule is not None
        assert rule.verification_status == "unverified"
        item = create_pending_review_item(
            session,
            subject_type="legal_rule",
            jurisdiction_id=rule.jurisdiction_id,
            legal_rule_id=rule.id,
            document_version_id=ver_id,
            review_reason="Post-upgrade governance item.",
            actor=_service_actor(),
        )
        session.commit()
        item_id = item.id

    # Downgrade removes governance tables but keeps corpus.
    engine.dispose()
    get_engine.cache_clear()
    command.downgrade(cfg, "20261118_0011")
    engine = get_engine()
    with engine.connect() as conn:
        isolation.assert_connected_to_test_database(conn)
        tables = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname='public' AND tablename LIKE 'legal_review_%'"
                )
            )
        }
        assert "legal_review_items" not in tables
        assert "legal_review_events" not in tables
        assert (
            conn.execute(
                text("SELECT count(*) FROM legal_rules WHERE id=:id"), {"id": rule_id}
            ).scalar_one()
            == 1
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM document_versions WHERE id=:id"), {"id": ver_id}
            ).scalar_one()
            == 1
        )
        chunk_count_after = conn.execute(
            text("SELECT count(*) FROM document_chunks")
        ).scalar_one()
        assert chunk_count_after == chunk_count_before

    # Re-upgrade for subsequent tests sharing the DB, then remove committed seeds.
    engine.dispose()
    get_engine.cache_clear()
    command.upgrade(cfg, "head")
    engine = get_engine()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with SessionLocal() as session:
        assert session.get(LegalRule, rule_id) is not None
        assert session.get(LegalReviewItem, item_id) is None
        isolation.clear_registry_tables(session)
        session.commit()


def test_live_lifepilot_database_unchanged():
    """Read-only check: live app DB must remain at 0010 (or at least without 0012)."""
    import os

    from sqlalchemy import create_engine

    app_url = os.environ.get(isolation.APP_DATABASE_URL_ENV)
    if not app_url:
        pytest.skip("LIFEPILOT_APP_DATABASE_URL not set")
    name = isolation.database_name(app_url)
    if name.endswith("_test"):
        pytest.skip("App URL points at a test DB")
    engine = create_engine(app_url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as conn:
            db = conn.execute(text("SELECT current_database()")).scalar_one()
            assert db == name
            assert not str(db).endswith("_test")
            version = conn.execute(
                text("SELECT version_num FROM alembic_version LIMIT 1")
            ).scalar_one()
            assert version != "20261122_0012"
            # Prefer documented live baseline when available.
            assert version in {"20261113_0010", "20261118_0011"}
            has_items = conn.execute(
                text("SELECT to_regclass('public.legal_review_items')")
            ).scalar_one()
            assert has_items is None
    finally:
        engine.dispose()
