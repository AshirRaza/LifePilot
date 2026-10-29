"""Explicit source review operations (separate from HTTP verification)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.db.models import (
    DOCUMENT_DISCOVERY_STATUSES,
    SOURCE_REVIEW_STATUSES,
    Document,
    Source,
)
from lifepilot.registry.urls import (
    InvalidRegistryUrlError,
    normalize_url,
    patterns_expand_scope,
    validate_allowlist_update,
    validate_http_url,
)


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
        try:
            # Seed must sit inside the allowlist; patterns may be ancestors of
            # the seed path (explicit scope) so long as the seed matches.
            validate_allowlist_update(
                source.seed_url,
                patterns,
                require_https=True,
            )
        except InvalidRegistryUrlError as exc:
            raise ReviewError(str(exc)) from exc

    source.review_status = status
    session.add(source)
    session.flush()
    return source


def set_source_seed_url(
    session: Session,
    *,
    source_id: int,
    seed_url: str,
) -> Source:
    """Explicitly update ``seed_url`` / ``seed_url_normalized`` for one source.

    Does not change ``review_status``. After a seed change, call
    ``set_source_allowed_patterns`` so the allowlist still covers the seed.
    """
    source = session.get(Source, source_id)
    if source is None:
        raise ReviewError("Source not found")
    try:
        normalized = validate_http_url(seed_url, require_https=True, allow_http=False)
    except InvalidRegistryUrlError as exc:
        raise ReviewError(str(exc)) from exc
    # Keep original presentation URL when provided; store normalized uniqueness key.
    source.seed_url = seed_url.strip()
    source.seed_url_normalized = normalize_url(normalized)
    session.add(source)
    session.flush()
    return source


def set_source_allowed_patterns(
    session: Session,
    *,
    source_id: int,
    patterns: list[str],
    confirm_scope_expansion: bool = False,
) -> Source:
    """Explicitly replace ``allowed_url_patterns`` for one source.

    Seed import never expands reviewed scopes. This is the manual path for
    intentional widenings (e.g. EU residence section). Requires
    ``confirm_scope_expansion=True`` when the new patterns enlarge scope.
    """
    source = session.get(Source, source_id)
    if source is None:
        raise ReviewError("Source not found")

    try:
        _, validated = validate_allowlist_update(
            source.seed_url,
            patterns,
            require_https=True,
        )
    except InvalidRegistryUrlError as exc:
        raise ReviewError(str(exc)) from exc
    existing = list(source.allowed_url_patterns or [])
    if patterns_expand_scope(existing, validated) and not confirm_scope_expansion:
        raise ReviewError(
            "new patterns expand crawl scope; pass confirm_scope_expansion=True "
            "after explicit review"
        )

    source.allowed_url_patterns = validated
    session.add(source)
    session.flush()
    return source


def set_document_discovery_status(
    session: Session,
    *,
    document_id: int,
    status: str,
) -> Document:
    """Explicitly set ``documents.discovery_status`` (not source review_status).

    Use ``rejected`` to exclude a document from discovery/downstream processing
    when stored HTML is unsuitable, without rejecting the official source.
    """
    if status not in DOCUMENT_DISCOVERY_STATUSES:
        raise ReviewError(
            f"Invalid discovery_status {status!r}; "
            f"expected one of {DOCUMENT_DISCOVERY_STATUSES}"
        )
    document = session.get(Document, document_id)
    if document is None:
        raise ReviewError("Document not found")
    document.discovery_status = status
    session.add(document)
    session.flush()
    return document
