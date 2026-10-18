"""Transactional helpers for document version currency.

When deciding whether content changed, compare the new SHA-256 only to the
current version (``is_current IS TRUE``), not to every historical version.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lifepilot.db.models import DocumentVersion


def get_current_version(
    session: Session, document_id: int
) -> DocumentVersion | None:
    """Return the current version row for a document, if any."""
    return session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.is_current.is_(True),
        )
    )


def set_current_version(session: Session, version: DocumentVersion) -> DocumentVersion:
    """Mark ``version`` as current and clear any previous current flag.

    Must be called inside an open transaction. Caller is responsible for commit.
    """
    session.execute(
        update(DocumentVersion)
        .where(
            DocumentVersion.document_id == version.document_id,
            DocumentVersion.is_current.is_(True),
            DocumentVersion.id != version.id,
        )
        .values(is_current=False)
    )
    version.is_current = True
    session.add(version)
    session.flush()
    return version


def content_matches_current(
    session: Session, document_id: int, content_sha256: str
) -> bool:
    """True if ``content_sha256`` equals the current version's hash."""
    current = get_current_version(session, document_id)
    if current is None:
        return False
    return current.content_sha256 == content_sha256
