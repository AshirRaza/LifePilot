"""PostgreSQL-backed discovery frontier for resumable BFS crawling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from lifepilot.crawl.url_filters import is_crawl_trap_url
from lifepilot.db.models import DiscoveryFrontier, Source
from lifepilot.registry.urls import (
    InvalidRegistryUrlError,
    normalize_url,
    url_in_any_allowed_pattern,
)

EnqueueOutcome = Literal[
    "inserted_pending",
    "inserted_out_of_scope",
    "inserted_skipped",
    "unchanged",
    "rejected",
]


@dataclass(frozen=True)
class EnqueueResult:
    outcome: EnqueueOutcome
    row: DiscoveryFrontier | None = None
    reason: str = ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _kind_hint_from_url(url: str) -> str:
    path = (urlsplit(url).path or "").lower()
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith(".docx"):
        return "docx"
    return "html_page"


def get_frontier_row(
    session: Session, source_id: int, url_normalized: str
) -> DiscoveryFrontier | None:
    return session.scalar(
        select(DiscoveryFrontier).where(
            DiscoveryFrontier.source_id == source_id,
            DiscoveryFrontier.url_normalized == url_normalized,
        )
    )


def enqueue_url(
    session: Session,
    source: Source,
    *,
    url: str,
    discovery_method: str,
    depth: int,
    parent_url: str | None = None,
    content_kind_hint: str | None = None,
    apply_trap_filter: bool = True,
) -> EnqueueResult:
    """Idempotently insert a frontier row for ``source``.

    First-seen wins for depth/method. Does not create ``documents`` rows.
    """
    if depth < 0:
        raise ValueError("depth must be >= 0")
    try:
        normalized = normalize_url(url)
    except InvalidRegistryUrlError as exc:
        return EnqueueResult(outcome="rejected", reason=str(exc))

    existing = get_frontier_row(session, source.id, normalized)
    if existing is not None:
        return EnqueueResult(outcome="unchanged", row=existing, reason="already present")

    patterns = list(source.allowed_url_patterns or [])
    if not url_in_any_allowed_pattern(normalized, patterns):
        row = DiscoveryFrontier(
            source_id=source.id,
            url_normalized=normalized,
            url_original=url,
            parent_url=parent_url,
            discovery_method=discovery_method,
            depth=depth,
            content_kind_hint=content_kind_hint or _kind_hint_from_url(normalized),
            status="out_of_scope",
            last_error="outside approved allowlist",
            failure_class="permanent",
        )
        session.add(row)
        session.flush()
        return EnqueueResult(
            outcome="inserted_out_of_scope",
            row=row,
            reason="outside approved allowlist",
        )

    if apply_trap_filter and discovery_method != "seed":
        trapped, trap_reason = is_crawl_trap_url(normalized)
        if trapped:
            row = DiscoveryFrontier(
                source_id=source.id,
                url_normalized=normalized,
                url_original=url,
                parent_url=parent_url,
                discovery_method=discovery_method,
                depth=depth,
                content_kind_hint=content_kind_hint or _kind_hint_from_url(normalized),
                status="skipped",
                last_error=trap_reason,
                failure_class="permanent",
            )
            session.add(row)
            session.flush()
            return EnqueueResult(
                outcome="inserted_skipped", row=row, reason=trap_reason
            )

    row = DiscoveryFrontier(
        source_id=source.id,
        url_normalized=normalized,
        url_original=url,
        parent_url=parent_url,
        discovery_method=discovery_method,
        depth=depth,
        content_kind_hint=content_kind_hint or _kind_hint_from_url(normalized),
        status="pending",
    )
    session.add(row)
    session.flush()
    return EnqueueResult(outcome="inserted_pending", row=row)


def enqueue_seed(session: Session, source: Source) -> EnqueueResult:
    """Ensure the source seed URL is pending at depth 0."""
    return enqueue_url(
        session,
        source,
        url=source.seed_url_normalized or source.seed_url,
        discovery_method="seed",
        depth=0,
        parent_url=None,
        content_kind_hint="html_page",
        apply_trap_filter=False,
    )


def claim_pending(
    session: Session,
    source_id: int,
    *,
    limit: int,
    max_depth: int,
    now: datetime | None = None,
) -> list[DiscoveryFrontier]:
    """Return up to ``limit`` eligible pending rows in BFS order."""
    if limit < 1:
        return []
    now = now or _utcnow()
    stmt = (
        select(DiscoveryFrontier)
        .where(
            DiscoveryFrontier.source_id == source_id,
            DiscoveryFrontier.status == "pending",
            DiscoveryFrontier.depth <= max_depth,
            or_(
                DiscoveryFrontier.next_attempt_at.is_(None),
                DiscoveryFrontier.next_attempt_at <= now,
            ),
        )
        .order_by(DiscoveryFrontier.depth.asc(), DiscoveryFrontier.id.asc())
        .limit(limit)
    )
    return list(session.scalars(stmt).all())


def mark_fetched(
    session: Session,
    row: DiscoveryFrontier,
    *,
    document_id: int | None,
) -> None:
    row.status = "fetched"
    row.document_id = document_id
    row.last_error = None
    row.failure_class = None
    row.next_attempt_at = None
    row.last_attempted_at = _utcnow()
    row.attempt_count = int(row.attempt_count or 0) + 1
    session.flush()


def mark_skipped(
    session: Session,
    row: DiscoveryFrontier,
    *,
    reason: str,
    document_id: int | None = None,
) -> None:
    row.status = "skipped"
    row.last_error = reason
    row.failure_class = "permanent"
    row.next_attempt_at = None
    row.document_id = document_id
    row.last_attempted_at = _utcnow()
    row.attempt_count = int(row.attempt_count or 0) + 1
    session.flush()


def mark_blocked(
    session: Session,
    row: DiscoveryFrontier,
    *,
    reason: str,
    document_id: int | None = None,
) -> None:
    row.status = "blocked"
    row.last_error = reason
    row.failure_class = "permanent"
    row.next_attempt_at = None
    row.document_id = document_id
    row.last_attempted_at = _utcnow()
    row.attempt_count = int(row.attempt_count or 0) + 1
    session.flush()


def mark_permanent_failure(
    session: Session,
    row: DiscoveryFrontier,
    *,
    reason: str,
    document_id: int | None = None,
) -> None:
    row.status = "failed"
    row.last_error = reason
    row.failure_class = "permanent"
    row.next_attempt_at = None
    row.document_id = document_id
    row.last_attempted_at = _utcnow()
    row.attempt_count = int(row.attempt_count or 0) + 1
    session.flush()


def mark_transient_failure(
    session: Session,
    row: DiscoveryFrontier,
    *,
    reason: str,
    max_attempts: int,
    base_backoff_seconds: float = 30.0,
    retry_after_seconds: float | None = None,
) -> str:
    """Keep URL eligible (pending) or permanently fail after ``max_attempts``.

    Returns ``\"pending\"`` or ``\"failed\"``.
    """
    now = _utcnow()
    attempts = int(row.attempt_count or 0) + 1
    row.attempt_count = attempts
    row.last_attempted_at = now
    row.last_error = reason
    row.failure_class = "transient"
    row.document_id = row.document_id

    if attempts >= max_attempts:
        row.status = "failed"
        row.next_attempt_at = None
        row.last_error = f"{reason} (exhausted {attempts} attempts)"
        session.flush()
        return "failed"

    if retry_after_seconds is not None and retry_after_seconds > 0:
        delay = float(retry_after_seconds)
    else:
        # Exponential backoff: 30s, 60s, 120s…
        delay = base_backoff_seconds * (2 ** max(0, attempts - 1))
    # Cap backoff at 1 hour for runaway Retry-After values.
    delay = min(delay, 3600.0)
    row.status = "pending"
    row.next_attempt_at = now + timedelta(seconds=delay)
    session.flush()
    return "pending"


def count_frontier(
    session: Session,
    source_ids: list[int],
    *,
    status: str | None = None,
) -> int:
    if not source_ids:
        return 0
    stmt = select(func.count()).select_from(DiscoveryFrontier).where(
        DiscoveryFrontier.source_id.in_(source_ids)
    )
    if status is not None:
        stmt = stmt.where(DiscoveryFrontier.status == status)
    return int(session.scalar(stmt) or 0)


def count_pending_eligible(
    session: Session,
    source_ids: list[int],
    *,
    max_depth: int,
    now: datetime | None = None,
) -> int:
    """Pending URLs at or below max_depth (including backoff-not-yet-due)."""
    if not source_ids:
        return 0
    now = now or _utcnow()
    # Count all pending within depth — including future next_attempt_at —
    # so completeness does not claim done while retries remain.
    stmt = (
        select(func.count())
        .select_from(DiscoveryFrontier)
        .where(
            DiscoveryFrontier.source_id.in_(source_ids),
            DiscoveryFrontier.status == "pending",
            DiscoveryFrontier.depth <= max_depth,
        )
    )
    return int(session.scalar(stmt) or 0)


def count_pending_due(
    session: Session,
    source_id: int,
    *,
    max_depth: int,
    now: datetime | None = None,
) -> int:
    now = now or _utcnow()
    stmt = (
        select(func.count())
        .select_from(DiscoveryFrontier)
        .where(
            DiscoveryFrontier.source_id == source_id,
            DiscoveryFrontier.status == "pending",
            DiscoveryFrontier.depth <= max_depth,
            or_(
                DiscoveryFrontier.next_attempt_at.is_(None),
                DiscoveryFrontier.next_attempt_at <= now,
            ),
        )
    )
    return int(session.scalar(stmt) or 0)


__all__ = [
    "EnqueueResult",
    "claim_pending",
    "count_frontier",
    "count_pending_due",
    "count_pending_eligible",
    "enqueue_seed",
    "enqueue_url",
    "get_frontier_row",
    "mark_blocked",
    "mark_fetched",
    "mark_permanent_failure",
    "mark_skipped",
    "mark_transient_failure",
]
