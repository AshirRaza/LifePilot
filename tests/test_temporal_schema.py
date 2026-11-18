"""Phase 10B.1 tests for the temporal legal data model and migration 0011.

All records used here are strictly synthetic test fixtures (never real rules).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from lifepilot.db import isolation
from lifepilot.db.models import (
    DOCUMENT_LEGAL_STATUSES,
    PLANNED_EMBEDDING_DIMENSIONS,
    PLANNED_EMBEDDING_MODEL_ID,
    Authority,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    DocumentVersion,
    Jurisdiction,
    LegalRule,
    Source,
)
from lifepilot.db.session import get_engine


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _seed_graph(session: Session, *, code: str = "XX-TEMP") -> dict:
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
    )
    session.add(ver)
    session.flush()
    return {
        "jurisdiction": jur,
        "authority": auth,
        "source": source,
        "document": doc,
        "version": ver,
    }


def _make_rule(g: dict, **overrides) -> LegalRule:
    payload = {
        "rule_key": "xx_route.synthetic_threshold",
        "jurisdiction_id": g["jurisdiction"].id,
        "jurisdiction_code": g["jurisdiction"].code,
        "pathway_id": "xx_route",
        "requirement_category": "financial_or_sponsorship",
        "requirement_type": "salary_threshold",
        "rule_summary": "Synthetic threshold rule for schema testing.",
        "document_id": g["document"].id,
        "document_version_id": g["version"].id,
        "source_id": g["source"].id,
    }
    payload.update(overrides)
    return LegalRule(**payload)


def test_document_version_existing_defaults_and_compatibility(db_session: Session):
    g = _seed_graph(db_session, code="XX-COMPAT")
    ver: DocumentVersion = g["version"]
    db_session.refresh(ver)

    assert ver.legal_status == "unknown"
    assert ver.is_current is True
    assert ver.http_last_modified is None
    assert ver.http_etag is None
    assert ver.source_published_at is None
    assert ver.source_updated_at is None
    assert ver.archived_at is None
    assert ver.superseded_at is None
    assert ver.superseded_by_version_id is None
    assert ver.temporal_notes is None


@pytest.mark.parametrize("status", DOCUMENT_LEGAL_STATUSES)
def test_document_version_legal_status_enum_values(db_session: Session, status: str):
    g = _seed_graph(db_session, code=f"XX-S-{status[:8]}")
    ver = DocumentVersion(
        document_id=g["document"].id,
        fetched_via_source_id=g["source"].id,
        version_number=2,
        source_url=g["document"].canonical_url,
        retrieved_at=datetime.now(timezone.utc),
        content_sha256=_sha(status),
        storage_path=f"data/raw/test/{status}.html",
        is_current=False,
        legal_status=status,
    )
    db_session.add(ver)
    db_session.flush()
    assert ver.legal_status == status


def test_archived_and_proposed_document_version_representation(db_session: Session):
    """is_current=True with legal_status='archived' stays valid and distinct."""
    g = _seed_graph(db_session, code="XX-ARCH")
    current_archived: DocumentVersion = g["version"]
    current_archived.legal_status = "archived"
    current_archived.archived_at = datetime(2026, 1, 15, tzinfo=timezone.utc)
    current_archived.http_last_modified = datetime(2024, 6, 1, tzinfo=timezone.utc)
    current_archived.http_etag = '"etag-arch-1"'
    current_archived.source_published_at = datetime(2023, 1, 1, tzinfo=timezone.utc)
    current_archived.source_updated_at = datetime(2024, 5, 31, tzinfo=timezone.utc)
    current_archived.temporal_notes = "Observed archived notice URL."

    proposed = DocumentVersion(
        document_id=g["document"].id,
        fetched_via_source_id=g["source"].id,
        version_number=2,
        source_url=g["document"].canonical_url,
        retrieved_at=datetime.now(timezone.utc),
        content_sha256=_sha("proposed-v2"),
        storage_path="data/raw/test/proposed_v2.html",
        is_current=False,
        legal_status="proposed",
        superseded_by_version_id=None,
    )
    db_session.add(proposed)
    db_session.flush()

    assert current_archived.is_current is True
    assert current_archived.legal_status == "archived"
    assert proposed.legal_status == "proposed"


def test_document_version_rejects_invalid_status_and_self_supersession(
    db_session: Session,
):
    g = _seed_graph(db_session, code="XX-BADVER")
    ver: DocumentVersion = g["version"]

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            ver.legal_status = "legally_certified"
            db_session.flush()
    db_session.refresh(ver)

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            ver.superseded_by_version_id = ver.id
            db_session.flush()
    db_session.refresh(ver)

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            ver.http_etag = "   "
            db_session.flush()
    db_session.refresh(ver)


def test_legal_rule_decimal_precision_and_currency_constraints(db_session: Session):
    g = _seed_graph(db_session, code="XX-DEC")
    exact_amount = Decimal("423.4500")
    max_amount = Decimal("99999.1234")
    rule = _make_rule(
        g,
        numeric_value=exact_amount,
        numeric_max_value=max_amount,
        numeric_comparator="gte",
        currency_code="EUR",
        amount_unit="monthly",
    )
    db_session.add(rule)
    db_session.flush()
    db_session.expire_all()

    loaded = db_session.get(LegalRule, rule.id)
    assert loaded is not None
    assert isinstance(loaded.numeric_value, Decimal)
    assert loaded.numeric_value == Decimal("423.4500")
    assert loaded.numeric_max_value == Decimal("99999.1234")
    assert loaded.currency_code == "EUR"

    # Negative amount rejected.
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g,
                    rule_key="xx_route.bad_neg",
                    numeric_value=Decimal("-1.00"),
                    currency_code="GBP",
                )
            )
            db_session.flush()

    # Lowercase currency rejected.
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g,
                    rule_key="xx_route.bad_cur",
                    numeric_value=Decimal("100.00"),
                    currency_code="gbp",
                )
            )
            db_session.flush()

    # Currency without numeric_value rejected.
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g,
                    rule_key="xx_route.bad_cur_nonum",
                    numeric_value=None,
                    currency_code="GBP",
                )
            )
            db_session.flush()

    # Unit without numeric_value rejected.
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g,
                    rule_key="xx_route.bad_unit_nonum",
                    numeric_value=None,
                    amount_unit="yearly",
                )
            )
            db_session.flush()

    # Inverted range rejected.
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g,
                    rule_key="xx_route.bad_range",
                    numeric_value=Decimal("500.00"),
                    numeric_max_value=Decimal("400.00"),
                )
            )
            db_session.flush()


def test_legal_rule_date_boundary_and_unknown_date_constraints(db_session: Session):
    g = _seed_graph(db_session, code="XX-DATES")

    # Unknown effective dates explicitly allowed when both dates NULL.
    unknown_rule = _make_rule(
        g,
        rule_key="xx_route.unknown_dates",
        effective_date_status="unknown",
        effective_from=None,
        effective_to=None,
    )
    db_session.add(unknown_rule)
    db_session.flush()
    assert unknown_rule.effective_date_status == "unknown"

    # Partially known effective date allowed without forcing an open-ended claim.
    partial_rule = _make_rule(
        g,
        rule_key="xx_route.partial_dates",
        effective_date_status="partially_known",
        effective_from=date(2024, 4, 4),
        effective_to=None,
    )
    db_session.add(partial_rule)
    db_session.flush()

    # Half-open exclusive upper bound [2024-04-04, 2030-04-04) allowed.
    half_open = _make_rule(
        g,
        rule_key="xx_route.half_open",
        effective_date_status="known_bounded",
        effective_from=date(2024, 4, 4),
        effective_to=date(2030, 4, 4),
        effective_from_inclusive=True,
        effective_to_inclusive=False,
    )
    db_session.add(half_open)
    db_session.flush()

    # Closed single-day [d, d] allowed.
    same_day_closed = _make_rule(
        g,
        rule_key="xx_route.same_day_closed",
        effective_date_status="known_bounded",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 1, 1),
        effective_from_inclusive=True,
        effective_to_inclusive=True,
    )
    db_session.add(same_day_closed)
    db_session.flush()

    # Empty half-open [d, d) rejected.
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g,
                    rule_key="xx_route.empty_half_open",
                    effective_date_status="known_bounded",
                    effective_from=date(2026, 1, 1),
                    effective_to=date(2026, 1, 1),
                    effective_from_inclusive=True,
                    effective_to_inclusive=False,
                )
            )
            db_session.flush()

    # Inverted interval rejected.
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g,
                    rule_key="xx_route.inverted",
                    effective_date_status="known_bounded",
                    effective_from=date(2026, 5, 1),
                    effective_to=date(2026, 4, 1),
                )
            )
            db_session.flush()

    # Unknown status cannot carry non-null date.
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g,
                    rule_key="xx_route.bad_unknown",
                    effective_date_status="unknown",
                    effective_from=date(2026, 1, 1),
                )
            )
            db_session.flush()

    # known_open_ended requires effective_from.
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g,
                    rule_key="xx_route.bad_open",
                    effective_date_status="known_open_ended",
                    effective_from=None,
                )
            )
            db_session.flush()

    # known_bounded requires effective_to.
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g,
                    rule_key="xx_route.bad_bounded",
                    effective_date_status="known_bounded",
                    effective_from=date(2026, 1, 1),
                    effective_to=None,
                )
            )
            db_session.flush()


def test_coexisting_cohort_thresholds_and_revision_lineage(db_session: Session):
    """General and transitional thresholds coexist without collision."""
    g = _seed_graph(db_session, code="XX-COHORT")

    rev1_general = _make_rule(
        g,
        rule_key="xx_route.salary.general",
        rule_revision=1,
        numeric_value=Decimal("30000.00"),
        currency_code="GBP",
        amount_unit="yearly",
        effective_date_status="known_bounded",
        effective_from=date(2024, 1, 1),
        effective_to=date(2025, 12, 31),
        cohort_key="general",
        cohort_category="general",
        is_transitional=False,
        cohort_conditions={"applicant_type": "standard"},
        rule_summary="Synthetic prior general threshold.",
    )
    db_session.add(rev1_general)
    db_session.flush()

    rev2_general = _make_rule(
        g,
        rule_key="xx_route.salary.general",
        rule_revision=2,
        supersedes_rule_id=rev1_general.id,
        numeric_value=Decimal("35000.00"),
        currency_code="GBP",
        amount_unit="yearly",
        effective_date_status="known_open_ended",
        effective_from=date(2026, 1, 1),
        effective_to=None,
        cohort_key="general",
        cohort_category="general",
        is_transitional=False,
        cohort_conditions={"applicant_type": "standard"},
        rule_summary="Synthetic updated general threshold.",
    )
    transitional_cohort = _make_rule(
        g,
        rule_key="xx_route.salary.transitional_cohort",
        rule_revision=1,
        numeric_value=Decimal("25000.00"),
        currency_code="GBP",
        amount_unit="yearly",
        effective_date_status="known_bounded",
        effective_from=date(2026, 1, 1),
        effective_to=date(2030, 1, 1),
        effective_to_inclusive=False,
        cohort_key="transitional_legacy",
        cohort_category="transitional",
        is_transitional=True,
        cohort_conditions={
            "prior_permission_before": "2024-01-01",
            "continuous_status": True,
        },
        rule_summary="Synthetic coexisting transitional cohort threshold.",
        conditions_text="Applies only to synthetic legacy cohort before 2030-01-01.",
    )
    db_session.add_all([rev2_general, transitional_cohort])
    db_session.flush()

    rows = db_session.scalars(
        select(LegalRule).where(LegalRule.pathway_id == "xx_route")
    ).all()
    assert len(rows) == 3

    # Clear helper respects LegalRule foreign keys.
    isolation.clear_registry_tables(db_session)
    assert len(db_session.scalars(select(LegalRule)).all()) == 0


def test_source_version_provenance_and_referential_integrity(db_session: Session):
    g1 = _seed_graph(db_session, code="XX-PROV1")
    g2 = _seed_graph(db_session, code="XX-PROV2")

    # Missing all provenance references rejected.
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g1,
                    rule_key="xx_route.no_prov",
                    source_id=None,
                    document_id=None,
                    document_version_id=None,
                )
            )
            db_session.flush()

    # Cross-document version mismatch rejected by trigger.
    with pytest.raises(DBAPIError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g1,
                    rule_key="xx_route.mismatch_doc_ver",
                    document_id=g1["document"].id,
                    document_version_id=g2["version"].id,
                )
            )
            db_session.flush()

    # Jurisdiction code mismatch rejected by trigger.
    with pytest.raises(DBAPIError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g1,
                    rule_key="xx_route.mismatch_jur",
                    jurisdiction_id=g1["jurisdiction"].id,
                    jurisdiction_code="XX-PROV2",
                )
            )
            db_session.flush()

    # Missing FK target rejected.
    with pytest.raises(DBAPIError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g1,
                    rule_key="xx_route.bad_fk",
                    document_version_id=9_999_999,
                )
            )
            db_session.flush()


def test_unsupported_verification_defaults_and_audit_guard(db_session: Session):
    """Referencing an official document never auto-verifies a rule."""
    g = _seed_graph(db_session, code="XX-VERIF")
    rule = _make_rule(g, rule_key="xx_route.unverified_default")
    db_session.add(rule)
    db_session.flush()
    db_session.refresh(rule)

    assert rule.review_status == "unreviewed"
    assert rule.verification_status == "unverified"

    # human_verified without reviewer/timestamp/approved/version rejected.
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g,
                    rule_key="xx_route.bad_hv_unapproved",
                    verification_status="human_verified",
                    review_status="unreviewed",
                    reviewed_by="reviewer_1",
                    reviewed_at=datetime.now(timezone.utc),
                )
            )
            db_session.flush()

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                _make_rule(
                    g,
                    rule_key="xx_route.bad_hv_nover",
                    verification_status="human_verified",
                    review_status="approved",
                    reviewed_by="reviewer_1",
                    reviewed_at=datetime.now(timezone.utc),
                    document_version_id=None,
                )
            )
            db_session.flush()

    # Complete audit metadata + pinned version succeeds.
    good_hv = _make_rule(
        g,
        rule_key="xx_route.good_hv",
        verification_status="human_verified",
        review_status="approved",
        reviewed_by="legal_reviewer_1",
        reviewed_at=datetime.now(timezone.utc),
        verification_notes="Synthetic audit-complete approval.",
    )
    db_session.add(good_hv)
    db_session.flush()
    assert good_hv.verification_status == "human_verified"


def test_migration_upgrade_and_downgrade_preserves_existing_data(
    test_database_url: str,
):
    """Verify reversible 0011 upgrade/downgrade against lifepilot_test only."""
    isolation.assert_safe_test_database(test_database_url)
    engine = get_engine()

    # 1. Seed a pre-existing version, chunk, and embedding on test DB.
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    jur_id = auth_id = src_id = doc_id = ver_id = chunk_id = emb_id = None
    with factory() as s:
        isolation.assert_connected_to_test_database(s)
        g = _seed_graph(s, code="XX-MIG")
        jur_id = g["jurisdiction"].id
        auth_id = g["authority"].id
        src_id = g["source"].id
        doc_id = g["document"].id
        ver_id = g["version"].id

        chunk = DocumentChunk(
            document_id=doc_id,
            document_version_id=ver_id,
            source_id=src_id,
            jurisdiction_id=jur_id,
            jurisdiction_code="XX-MIG",
            chunk_index=0,
            content_text="Synthetic migration preservation passage.",
            content_sha256=_sha("mig-chunk"),
            is_current_version=True,
        )
        s.add(chunk)
        s.flush()
        chunk_id = chunk.id

        emb = DocumentChunkEmbedding(
            chunk_id=chunk_id,
            embedding_model_id=PLANNED_EMBEDDING_MODEL_ID,
            embedding=[0.01] * PLANNED_EMBEDDING_DIMENSIONS,
        )
        s.add(emb)
        s.commit()
        emb_id = emb.id

    cfg = Config(str(isolation.REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(isolation.REPO_ROOT / "alembic"))
    import os

    isolation.assert_safe_test_database(
        os.environ[isolation.DATABASE_URL_ENV],
        os.environ.get(isolation.APP_DATABASE_URL_ENV),
    )

    try:
        engine.dispose()
        # 2. Downgrade to 20261113_0010 and confirm clean schema rollback + data survival.
        command.downgrade(cfg, "20261113_0010")
        with engine.connect() as conn:
            isolation.assert_connected_to_test_database(conn)
            has_legal_rules = conn.execute(
                text("SELECT to_regclass('public.legal_rules')")
            ).scalar_one()
            assert has_legal_rules is None
            cols = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'document_versions'"
                    )
                ).fetchall()
            }
            assert "legal_status" not in cols
            assert "http_last_modified" not in cols
            assert conn.execute(
                text("SELECT COUNT(*) FROM document_versions WHERE id = :id"),
                {"id": ver_id},
            ).scalar_one() == 1
            assert conn.execute(
                text("SELECT COUNT(*) FROM document_chunks WHERE id = :id"),
                {"id": chunk_id},
            ).scalar_one() == 1
            assert conn.execute(
                text("SELECT COUNT(*) FROM document_chunk_embeddings WHERE id = :id"),
                {"id": emb_id},
            ).scalar_one() == 1

        engine.dispose()
        # 3. Re-upgrade to head and verify safe defaults + preserved rows.
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            isolation.assert_connected_to_test_database(conn)
            has_legal_rules = conn.execute(
                text("SELECT to_regclass('public.legal_rules')")
            ).scalar_one()
            assert has_legal_rules == "legal_rules"
            row = conn.execute(
                text(
                    "SELECT legal_status, is_current, retrieved_at "
                    "FROM document_versions WHERE id = :id"
                ),
                {"id": ver_id},
            ).one()
            assert row.legal_status == "unknown"
            assert row.is_current is True
            assert row.retrieved_at is not None
            assert conn.execute(
                text("SELECT COUNT(*) FROM document_chunks WHERE id = :id"),
                {"id": chunk_id},
            ).scalar_one() == 1
            assert conn.execute(
                text("SELECT COUNT(*) FROM document_chunk_embeddings WHERE id = :id"),
                {"id": emb_id},
            ).scalar_one() == 1
    finally:
        engine.dispose()
        command.upgrade(cfg, "head")
        with factory() as cleanup:
            isolation.assert_connected_to_test_database(cleanup)
            if emb_id is not None:
                cleanup.execute(
                    delete(DocumentChunkEmbedding).where(
                        DocumentChunkEmbedding.id == emb_id
                    )
                )
            if chunk_id is not None:
                cleanup.execute(
                    delete(DocumentChunk).where(DocumentChunk.id == chunk_id)
                )
            if ver_id is not None:
                cleanup.execute(
                    delete(DocumentVersion).where(DocumentVersion.id == ver_id)
                )
            if doc_id is not None:
                cleanup.execute(delete(Document).where(Document.id == doc_id))
            if src_id is not None:
                cleanup.execute(delete(Source).where(Source.id == src_id))
            if auth_id is not None:
                cleanup.execute(delete(Authority).where(Authority.id == auth_id))
            if jur_id is not None:
                cleanup.execute(delete(Jurisdiction).where(Jurisdiction.id == jur_id))
            cleanup.commit()
        engine.dispose()
