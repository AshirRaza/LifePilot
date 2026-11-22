"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from lifepilot.db.session import get_session_factory


def get_db() -> Iterator[Session]:
    """Request-scoped SQLAlchemy session (commit on success, rollback on error)."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
