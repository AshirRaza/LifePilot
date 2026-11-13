"""Pytest fixtures with a hard-isolated test database.

Database tests NEVER run against the application database (``lifepilot``).
At import time this module redirects ``DATABASE_URL`` to the test database
(``LIFEPILOT_TEST_DATABASE_URL`` or ``lifepilot_test`` derived from the app
URL), so ``get_engine()``, Alembic and any helper reach only the test DB. If
the resolved name is not positively a test DB (must end with ``_test``),
pytest aborts before any connection is made.

Create the test database once (optional - the session fixture does it too):

    docker compose exec postgres psql -U lifepilot -d postgres -c "CREATE DATABASE lifepilot_test"

See also: .env.example (LIFEPILOT_TEST_DATABASE_URL).
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from sqlalchemy import event, text
from sqlalchemy.orm import Session, sessionmaker

from lifepilot.db import isolation

# Load .env first (does not override real env vars), then redirect.
load_dotenv(isolation.REPO_ROOT / ".env")
try:
    TEST_DATABASE_URL = isolation.redirect_environment_to_test_database(os.environ)
except isolation.UnsafeTestDatabaseError as exc:  # fail closed, before any connect
    raise pytest.UsageError(f"Unsafe test database configuration: {exc}") from exc

from lifepilot.db.base import Base  # noqa: E402
from lifepilot.db.session import get_engine  # noqa: E402


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Create (if missing), migrate and reset the isolated test database."""
    app_url = os.environ.get(isolation.APP_DATABASE_URL_ENV)
    isolation.assert_safe_test_database(TEST_DATABASE_URL, app_url)
    isolation.ensure_test_database_exists(TEST_DATABASE_URL, app_url)
    isolation.migrate_test_database(TEST_DATABASE_URL)

    get_engine.cache_clear()
    engine = get_engine()
    expected = isolation.database_name(TEST_DATABASE_URL)
    assert isolation.database_name(engine.url) == expected
    with engine.begin() as conn:
        isolation.assert_connected_to_test_database(conn, expected)
        tables = ", ".join(
            f'"{t.name}"' for t in Base.metadata.sorted_tables
        )
        # Test DB only (guard above): start every run from empty tables.
        conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
        # Re-seed planned embedding model (migration seed is wiped by TRUNCATE).
        from lifepilot.db.models import (
            PLANNED_EMBEDDING_DIMENSIONS,
            PLANNED_EMBEDDING_MODEL_ID,
        )

        conn.execute(
            text(
                """
                INSERT INTO embedding_models
                    (id, display_name, dimensions, distance_metric, notes)
                VALUES
                    (:id, 'Multilingual E5 Small', :dims, 'cosine',
                     'Test DB seed after truncate')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": PLANNED_EMBEDDING_MODEL_ID, "dims": PLANNED_EMBEDDING_DIMENSIONS},
        )
    return TEST_DATABASE_URL


@pytest.fixture()
def db_session(test_database_url: str) -> Session:
    """Provide a session rolled back after each test (no lasting rows)."""
    engine = get_engine()
    connection = engine.connect()
    transaction = connection.begin()
    try:
        isolation.assert_connected_to_test_database(
            connection, isolation.database_name(test_database_url)
        )
    except Exception:
        transaction.rollback()
        connection.close()
        raise
    factory = sessionmaker(bind=connection, autoflush=False, autocommit=False)
    session = factory()
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess: Session, trans) -> None:  # type: ignore[no-untyped-def]
        if trans.nested and not trans._parent.nested:  # noqa: SLF001
            sess.begin_nested()

    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def reset_agent_runtime():
    """Reset agent config/graph/orchestrator/embedder/LLM caches around a test."""
    from lifepilot.agent.config import reset_agent_config_cache
    from lifepilot.agent.graph import reset_compiled_agent_graph
    from lifepilot.agent.orchestration import (
        reset_default_evidence_orchestrator,
        reset_evidence_orchestrator_override,
    )
    from lifepilot.agent.providers import (
        reset_llm_provider_cache,
        reset_llm_provider_override,
    )
    from lifepilot.rag.embedder import reset_shared_embedder

    reset_agent_config_cache()
    reset_compiled_agent_graph()
    reset_evidence_orchestrator_override()
    reset_default_evidence_orchestrator()
    reset_llm_provider_override()
    reset_llm_provider_cache()
    reset_shared_embedder()
    yield
    reset_evidence_orchestrator_override()
    reset_default_evidence_orchestrator()
    reset_llm_provider_override()
    reset_llm_provider_cache()
    reset_shared_embedder()
    reset_agent_config_cache()
    reset_compiled_agent_graph()
