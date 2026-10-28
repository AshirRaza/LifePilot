"""Shared helpers for locating crawl source documents."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.db.models import Document, Source


def document_for_source_seed(session: Session, source: Source) -> Document | None:
    """Return the document whose normalized URL matches the source seed URL."""
    return session.scalar(
        select(Document).where(Document.url_normalized == source.seed_url_normalized)
    )
