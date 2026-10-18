"""Explicit source review operations (separate from HTTP verification)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.db.models import SOURCE_REVIEW_STATUSES, Source
from lifepilot.registry.urls import validate_seed_and_patterns


class ReviewError(ValueError):
    """Invalid explicit review operation."""


def set_source_review_status(
    session: Session,
    *,
    status: str,
    source_id: int | None = None,
    seed_url_normalized: str | None = None,
) -> Source:
    """Explicitly set ``review_status`` for a source.

    Import/HTTP verification must never call this. Approving a source requires
    HTTPS seed URL and validated allowlist patterns.
    """
    if status not in SOURCE_REVIEW_STATUSES:
        raise ReviewError(
            f"Invalid review status {status!r}; "
            f"expected one of {SOURCE_REVIEW_STATUSES}"
        )
    if (source_id is None) == (seed_url_normalized is None):
        raise ReviewError("Provide exactly one of source_id or seed_url_normalized")

    if source_id is not None:
        source = session.get(Source, source_id)
    else:
        source = session.scalar(
            select(Source).where(Source.seed_url_normalized == seed_url_normalized)
        )

    if source is None:
        raise ReviewError("Source not found")

    if status == "approved":
        patterns = list(source.allowed_url_patterns or [])
        validate_seed_and_patterns(
            source.seed_url,
            patterns,
            require_https=True,
        )

    source.review_status = status
    session.add(source)
    session.flush()
    return source
