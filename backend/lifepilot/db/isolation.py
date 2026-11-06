"""Fail-closed isolation helpers for database-backed tests.

Destructive tests (``DELETE FROM sources`` etc.) must never run against the
application database. This module:

* resolves the test database URL (``LIFEPILOT_TEST_DATABASE_URL`` or the
  application URL with the database name replaced by ``lifepilot_test``);
* refuses any database that is not positively identified as a test database;
* creates the test database if missing (only after the guard passes) and
  migrates it with Alembic;
* verifies, on a live connection, that ``current_database()`` is the expected
  test database before any destructive statement.

Create the test database manually (optional; the fixtures also do this):

    docker compose exec postgres psql -U lifepilot -d postgres \
        -c "CREATE DATABASE lifepilot_test"
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import Connection, create_engine, delete, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from lifepilot.db.url import normalize_database_url

DEFAULT_TEST_DATABASE_NAME = "lifepilot_test"
TEST_DATABASE_URL_ENV = "LIFEPILOT_TEST_DATABASE_URL"
APP_DATABASE_URL_ENV = "LIFEPILOT_APP_DATABASE_URL"
DATABASE_URL_ENV = "DATABASE_URL"

# Positive identification: lowercase identifier that ends in "_test".
_TEST_DB_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*_test$")
_FORBIDDEN_DB_NAMES = frozenset({"postgres", "template0", "template1"})
_MAINTENANCE_DB = "postgres"

REPO_ROOT = Path(__file__).resolve().parents[3]


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when a test would touch a database not identified as a test DB."""


def database_name(url: str | URL) -> str:
    name = make_url(url).database
    if not name:
        raise UnsafeTestDatabaseError("Database URL has no database name.")
    return name


def assert_safe_test_database(
    test_url: str | URL,
    app_url: str | URL | None = None,
) -> None:
    """Fail closed unless ``test_url`` positively identifies a test database."""
    name = database_name(test_url)
    if name in _FORBIDDEN_DB_NAMES:
        raise UnsafeTestDatabaseError(
            f"Refusing to use system database {name!r} for tests."
        )
    if not _TEST_DB_NAME_RE.match(name):
        raise UnsafeTestDatabaseError(
            f"Refusing to run tests against database {name!r}: the name must "
            f"end with '_test' (e.g. {DEFAULT_TEST_DATABASE_NAME!r})."
        )
    if app_url is not None:
        app_url = make_url(app_url)
        test = make_url(test_url)
        same_server = (app_url.host, app_url.port) == (test.host, test.port)
        if app_url.database == name and same_server:
            raise UnsafeTestDatabaseError(
                f"Test database {name!r} is the application database."
            )


def resolve_test_database_url(environ: Mapping[str, str]) -> str:
    """Return the test DB URL from env, deriving it from the app URL if unset."""
    explicit = environ.get(TEST_DATABASE_URL_ENV)
    if explicit:
        return normalize_database_url(explicit)
    app_url = environ.get(APP_DATABASE_URL_ENV) or environ.get(DATABASE_URL_ENV)
    if not app_url:
        raise UnsafeTestDatabaseError(
            f"Set {TEST_DATABASE_URL_ENV} or {DATABASE_URL_ENV} to run DB tests."
        )
    return normalize_database_url(
        make_url(app_url).set(database=DEFAULT_TEST_DATABASE_NAME)
    )


def redirect_environment_to_test_database(environ: dict[str, str]) -> str:
    """Point ``DATABASE_URL`` at the test DB for this process; return that URL.

    The original application URL is preserved in ``LIFEPILOT_APP_DATABASE_URL``
    so the guard can compare against it. Idempotent. Any code path that calls
    ``get_engine()`` or Alembic's ``env.py`` therefore reaches the test DB only.
    """
    app_url = environ.get(APP_DATABASE_URL_ENV) or environ.get(DATABASE_URL_ENV)
    if app_url and APP_DATABASE_URL_ENV not in environ:
        # Preserve the original app URL string for guard comparisons / tests.
        environ[APP_DATABASE_URL_ENV] = app_url
    test_url = resolve_test_database_url(environ)
    assert_safe_test_database(test_url, environ.get(APP_DATABASE_URL_ENV))
    environ[DATABASE_URL_ENV] = normalize_database_url(test_url)
    return test_url


def ensure_test_database_exists(test_url: str | URL, app_url: str | URL | None) -> bool:
    """CREATE DATABASE for the (guard-verified) test DB if missing."""
    assert_safe_test_database(test_url, app_url)
    url = make_url(normalize_database_url(test_url))
    name = url.database
    assert name is not None
    admin = create_engine(
        url.set(database=_MAINTENANCE_DB),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 10},
    )
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
            if exists:
                return False
            # Name is validated against a strict identifier regex above.
            conn.execute(text(f'CREATE DATABASE "{name}"'))
            return True
    finally:
        admin.dispose()


def migrate_test_database(test_url: str | URL) -> None:
    """Run ``alembic upgrade head`` against the test DB only."""
    from alembic import command
    from alembic.config import Config

    assert_safe_test_database(test_url)
    previous = os.environ.get(DATABASE_URL_ENV)
    os.environ[DATABASE_URL_ENV] = normalize_database_url(test_url)
    try:
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop(DATABASE_URL_ENV, None)
        else:
            os.environ[DATABASE_URL_ENV] = previous


def assert_connected_to_test_database(
    connection: Connection | Session,
    expected_name: str | None = None,
) -> str:
    """Verify on the live connection that we are on a test DB (not ``lifepilot``)."""
    actual = connection.execute(text("SELECT current_database()")).scalar_one()
    assert_safe_test_database(
        URL.create("postgresql", database=actual),
    )
    if expected_name is not None and actual != expected_name:
        raise UnsafeTestDatabaseError(
            f"Connected to {actual!r} but expected test database {expected_name!r}."
        )
    app_url = os.environ.get(APP_DATABASE_URL_ENV)
    if app_url and make_url(app_url).database == actual:
        raise UnsafeTestDatabaseError(
            f"Connected to application database {actual!r}; refusing to proceed."
        )
    return actual


def clear_registry_tables(session: Session) -> None:
    """Delete all registry/corpus rows in FK-safe order (test DB only).

    ``discovery_frontier`` references both ``sources`` and ``documents`` and
    must be cleared first.
    """
    from lifepilot.db.models import (
        Authority,
        DiscoveryFrontier,
        Document,
        DocumentChunk,
        DocumentChunkEmbedding,
        DocumentContentComparison,
        DocumentExtraction,
        DocumentJurisdiction,
        DocumentVersion,
        EmbeddingModel,
        Jurisdiction,
        Source,
    )

    assert_connected_to_test_database(session)
    for model in (
        DocumentChunkEmbedding,
        DocumentChunk,
        EmbeddingModel,
        DiscoveryFrontier,
        DocumentContentComparison,
        DocumentExtraction,
        DocumentVersion,
        DocumentJurisdiction,
        Document,
        Source,
        Authority,
        Jurisdiction,
    ):
        session.execute(delete(model))
    session.flush()
