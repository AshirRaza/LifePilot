"""Regression tests for test-database isolation (Phase 6I Priority A)."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from lifepilot.db import isolation
from lifepilot.db.isolation import (
    UnsafeTestDatabaseError,
    assert_connected_to_test_database,
    assert_safe_test_database,
    clear_registry_tables,
    resolve_test_database_url,
)
from lifepilot.db.models import (
    Authority,
    DiscoveryFrontier,
    Document,
    Jurisdiction,
    Source,
)
from lifepilot.db.session import get_engine

APP_URL = "postgresql+psycopg://u:p@localhost:5432/lifepilot"
TEST_URL = "postgresql+psycopg://u:p@localhost:5432/lifepilot_test"


# --- (b) accidental connection to a non-test DB is refused ------------------


@pytest.mark.parametrize(
    "url",
    [
        APP_URL,
        "postgresql+psycopg://u:p@localhost:5432/postgres",
        "postgresql+psycopg://u:p@localhost:5432/template1",
        "postgresql+psycopg://u:p@localhost:5432/lifepilot_testing",
        "postgresql+psycopg://u:p@localhost:5432/test_lifepilot",
        "postgresql+psycopg://u:p@localhost:5432/LIFEPILOT_TEST",
        "postgresql+psycopg://u:p@localhost:5432/",
    ],
)
def test_guard_refuses_non_test_databases(url: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError):
        assert_safe_test_database(url, APP_URL)


def test_guard_accepts_test_database_and_rejects_app_clone() -> None:
    assert_safe_test_database(TEST_URL, APP_URL)
    # A "_test" name that is nevertheless the application DB is refused.
    app_named_test = "postgresql+psycopg://u:p@localhost:5432/lifepilot_test"
    with pytest.raises(UnsafeTestDatabaseError):
        assert_safe_test_database(TEST_URL, app_named_test)


def test_explicit_live_url_in_test_env_is_refused() -> None:
    environ = {
        isolation.TEST_DATABASE_URL_ENV: APP_URL,
        isolation.DATABASE_URL_ENV: APP_URL,
    }
    with pytest.raises(UnsafeTestDatabaseError):
        isolation.redirect_environment_to_test_database(environ)
    assert environ[isolation.DATABASE_URL_ENV] == APP_URL  # untouched


def test_redirect_derives_lifepilot_test_from_app_url() -> None:
    environ = {isolation.DATABASE_URL_ENV: APP_URL}
    assert resolve_test_database_url(environ).endswith("/lifepilot_test")
    isolation.redirect_environment_to_test_database(environ)
    assert environ[isolation.APP_DATABASE_URL_ENV] == APP_URL
    assert environ[isolation.DATABASE_URL_ENV].endswith("/lifepilot_test")


def test_create_database_refused_for_app_db_before_connecting() -> None:
    # Guard runs before any connection attempt, so this never touches a server.
    with pytest.raises(UnsafeTestDatabaseError):
        isolation.ensure_test_database_exists(APP_URL, APP_URL)
    with pytest.raises(UnsafeTestDatabaseError):
        isolation.migrate_test_database(APP_URL)


def test_process_engine_points_at_test_database(test_database_url: str) -> None:
    """get_engine() (used by app code under test) must never be the live DB."""
    app_db = isolation.database_name(os.environ[isolation.APP_DATABASE_URL_ENV])
    engine_db = isolation.database_name(get_engine().url)
    assert engine_db != app_db
    assert engine_db.endswith("_test")


def test_live_connection_guard_refuses_non_test_database(db_session) -> None:
    """The runtime check rejects a connection whose current_database() is not a test DB."""
    actual = db_session.execute(text("SELECT current_database()")).scalar_one()
    assert actual.endswith("_test")
    assert assert_connected_to_test_database(db_session) == actual
    with pytest.raises(UnsafeTestDatabaseError):
        assert_connected_to_test_database(db_session, expected_name="lifepilot")


# --- (a) original FK / frontier scenario works on the test DB ---------------


def _seed_source_document_frontier(session) -> None:
    country = Jurisdiction(
        code="XX-iso", name="Iso", jurisdiction_type="country", country_code="XX"
    )
    session.add(country)
    session.flush()
    authority = Authority(jurisdiction_id=country.id, name="Isolation Authority")
    session.add(authority)
    session.flush()
    seed = "https://www.example.test/isolation"
    source = Source(
        authority_id=authority.id,
        jurisdiction_id=country.id,
        name="isolation",
        seed_url=seed,
        seed_url_normalized=seed,
        allowed_url_patterns=[f"{seed}*"],
        review_status="approved",
    )
    session.add(source)
    session.flush()
    doc = Document(
        authority_id=authority.id,
        primary_source_id=source.id,
        canonical_url=seed,
        url_normalized=seed,
        content_kind="html_page",
        discovery_status="seed",
    )
    session.add(doc)
    session.flush()
    session.add(
        DiscoveryFrontier(
            source_id=source.id,
            url_normalized=seed,
            discovery_method="seed",
            status="fetched",
            document_id=doc.id,
        )
    )
    session.flush()


def test_clear_registry_handles_frontier_rows_on_test_db(db_session) -> None:
    """Original failure: DELETE FROM documents violated discovery_frontier FK."""
    _seed_source_document_frontier(db_session)
    assert db_session.scalar(select(func.count()).select_from(DiscoveryFrontier)) == 1

    clear_registry_tables(db_session)

    for model in (DiscoveryFrontier, Document, Source, Authority, Jurisdiction):
        assert db_session.scalar(select(func.count()).select_from(model)) == 0


def test_naive_delete_still_violates_fk_proving_the_scenario(db_session) -> None:
    """Document why the old helper failed: frontier must be cleared first."""
    from sqlalchemy import delete

    _seed_source_document_frontier(db_session)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(delete(Document))


def test_rows_do_not_leak_between_tests_a(db_session) -> None:
    _seed_source_document_frontier(db_session)


def test_rows_do_not_leak_between_tests_b(db_session) -> None:
    assert db_session.scalar(select(func.count()).select_from(DiscoveryFrontier)) == 0
    assert db_session.scalar(select(func.count()).select_from(Source)) == 0
