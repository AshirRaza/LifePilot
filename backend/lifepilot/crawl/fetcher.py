"""Approved-seed and bounded in-scope HTML fetcher.

Rechecks approval, allowlist, robots, and policy before each fetch.
Persists HTML under ``data/raw/documents/...`` and records document versions.
Link discovery is capped separately; this module fetches one URL at a time.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lifepilot.crawl.html_quality import (
    OUTCOME_BLOCKED_BY_BOT_PROTECTION,
    OUTCOME_INVALID_CONTENT,
    assess_html_content,
)
from lifepilot.crawl.http_fetch import fetch_html_seed
from lifepilot.crawl.planner import DECISION_PLANNED, evaluate_source
from lifepilot.crawl.policy import CrawlPolicy, fetcher_policy
from lifepilot.crawl.storage import (
    data_root,
    delete_quiet,
    relative_storage_path,
    version_file_path,
    write_bytes_atomic,
)
from lifepilot.db.current_version import content_matches_current, set_current_version
from lifepilot.db.models import Document, DocumentVersion, Source
from lifepilot.registry.urls import (
    InvalidRegistryUrlError,
    normalize_url,
    url_in_any_allowed_pattern,
)


STATUS_DRY_RUN = "dry_run"
STATUS_FETCHED_NEW_VERSION = "fetched_new_version"
STATUS_UNCHANGED = "unchanged"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_BLOCKED_BY_BOT_PROTECTION = OUTCOME_BLOCKED_BY_BOT_PROTECTION
STATUS_INVALID_CONTENT = OUTCOME_INVALID_CONTENT

_UNSUCCESSFUL_PERSIST = {
    STATUS_FAILED,
    STATUS_BLOCKED_BY_BOT_PROTECTION,
    STATUS_INVALID_CONTENT,
}


@dataclass
class SeedFetchResult:
    source_id: int
    source_name: str
    seed_url: str
    status: str
    document_id: int | None = None
    version_id: int | None = None
    version_number: int | None = None
    content_sha256: str | None = None
    storage_path: str | None = None
    http_status: int | None = None
    final_url: str | None = None
    message: str = ""


@dataclass
class SeedFetchReport:
    results: list[SeedFetchResult] = field(default_factory=list)

    @property
    def fetched_new(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_FETCHED_NEW_VERSION)

    @property
    def unchanged(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_UNCHANGED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status in _UNSUCCESSFUL_PERSIST)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_SKIPPED)

    @property
    def dry_run(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_DRY_RUN)

    @property
    def blocked(self) -> int:
        return sum(
            1
            for r in self.results
            if r.status
            in {STATUS_BLOCKED_BY_BOT_PROTECTION, STATUS_INVALID_CONTENT}
        )


def _get_source(session: Session, source_id: int) -> Source | None:
    return session.get(Source, source_id)


def _next_version_number(session: Session, document_id: int) -> int:
    current_max = session.scalar(
        select(func.max(DocumentVersion.version_number)).where(
            DocumentVersion.document_id == document_id
        )
    )
    return int(current_max or 0) + 1


def _get_or_create_document(
    session: Session,
    source: Source,
    *,
    canonical_url: str,
    url_normalized: str,
    discovery_status: str,
) -> Document:
    """Return the document for ``url_normalized``, creating it if needed.

    Existing rows are reused as-is. ``primary_source_id`` is first-owner
    provenance and must not be rewritten when another approved source later
    fetches the same URL. Per-fetch attribution lives on
    ``document_versions.fetched_via_source_id``.
    """
    existing = session.scalar(
        select(Document).where(Document.url_normalized == url_normalized)
    )
    if existing is not None:
        return existing

    doc = Document(
        authority_id=source.authority_id,
        primary_source_id=source.id,
        canonical_url=canonical_url,
        url_normalized=url_normalized,
        content_kind="html_page",
        language=source.default_language,
        title=None,
        discovery_status=discovery_status,
    )
    session.add(doc)
    session.flush()
    return doc


def _persist_html_version(
    session: Session,
    *,
    source: Source,
    requested_url: str,
    url_normalized: str,
    discovery_status: str,
    html_body: bytes,
    content_type: str | None,
    http_status: int | None,
    final_url: str | None,
    storage_root: Path,
) -> SeedFetchResult:
    quality = assess_html_content(html_body)
    if not quality.ok:
        existing = session.scalar(
            select(Document).where(Document.url_normalized == url_normalized)
        )
        return SeedFetchResult(
            source_id=source.id,
            source_name=source.name,
            seed_url=requested_url,
            status=quality.outcome,
            document_id=existing.id if existing is not None else None,
            content_sha256=hashlib.sha256(html_body).hexdigest(),
            http_status=http_status,
            final_url=final_url,
            message=(
                f"{quality.message}; not stored as a document version "
                "(previous versions left unchanged)"
            ),
        )

    digest = hashlib.sha256(html_body).hexdigest()
    document = _get_or_create_document(
        session,
        source,
        canonical_url=requested_url,
        url_normalized=url_normalized,
        discovery_status=discovery_status,
    )

    if content_matches_current(session, document.id, digest):
        session.flush()
        return SeedFetchResult(
            source_id=source.id,
            source_name=source.name,
            seed_url=requested_url,
            status=STATUS_UNCHANGED,
            document_id=document.id,
            content_sha256=digest,
            http_status=http_status,
            final_url=final_url,
            message="content hash matches current version; no new version created",
        )

    version_number = _next_version_number(session, document.id)
    abs_path = version_file_path(storage_root, document.id, version_number)
    rel_path = relative_storage_path(storage_root, abs_path)

    write_bytes_atomic(abs_path, html_body)
    try:
        version = DocumentVersion(
            document_id=document.id,
            fetched_via_source_id=source.id,
            version_number=version_number,
            source_url=final_url or requested_url,
            content_type=_content_type_or_html(content_type),
            language=source.default_language,
            retrieved_at=datetime.now(timezone.utc),
            http_status=http_status,
            content_sha256=digest,
            byte_size=len(html_body),
            storage_path=rel_path,
            is_current=False,
        )
        session.add(version)
        session.flush()
        set_current_version(session, version)
        session.flush()
    except Exception:
        delete_quiet(abs_path)
        raise

    return SeedFetchResult(
        source_id=source.id,
        source_name=source.name,
        seed_url=requested_url,
        status=STATUS_FETCHED_NEW_VERSION,
        document_id=document.id,
        version_id=version.id,
        version_number=version_number,
        content_sha256=digest,
        storage_path=rel_path,
        http_status=http_status,
        final_url=final_url,
        message="stored new HTML document version",
    )


def fetch_html_document_url(
    session: Session,
    source: Source,
    *,
    url: str,
    discovery_status: str = "discovered",
    persist: bool = False,
    policy: CrawlPolicy | None = None,
    http_client=None,
    storage_root: Path | str | None = None,
    apply_rate_limit: bool = False,
    last_host_fetch_at: dict[str, float] | None = None,
) -> SeedFetchResult:
    """Fetch one in-allowlist HTML URL under an approved source.

    Used by bounded discovery. Does not follow further links itself.
    """
    policy = policy or fetcher_policy()
    root = data_root(storage_root)
    session.refresh(source)

    if source.review_status != "approved":
        return SeedFetchResult(
            source_id=source.id,
            source_name=source.name,
            seed_url=url,
            status=STATUS_SKIPPED,
            message=f"source review_status={source.review_status!r}; not approved",
        )

    patterns = list(source.allowed_url_patterns or [])
    try:
        url_normalized = normalize_url(url)
    except InvalidRegistryUrlError as exc:
        return SeedFetchResult(
            source_id=source.id,
            source_name=source.name,
            seed_url=url,
            status=STATUS_FAILED,
            message=f"invalid URL: {exc}",
        )

    if not url_in_any_allowed_pattern(url_normalized, patterns):
        return SeedFetchResult(
            source_id=source.id,
            source_name=source.name,
            seed_url=url_normalized,
            status=STATUS_SKIPPED,
            message="URL outside approved allowlist scope",
        )

    if not persist:
        return SeedFetchResult(
            source_id=source.id,
            source_name=source.name,
            seed_url=url_normalized,
            status=STATUS_DRY_RUN,
            message=(
                "dry-run: would fetch in-scope HTML after allowlist check; "
                "pass --persist to download and store"
            ),
        )

    host = urlsplit(url_normalized).hostname or ""
    if apply_rate_limit and last_host_fetch_at is not None and host:
        delay = policy.default_crawl_delay_seconds
        last = last_host_fetch_at.get(host)
        if last is not None:
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)

    html = fetch_html_seed(
        url_normalized,
        allowed_patterns=patterns,
        policy=policy,
        client=http_client,
    )
    if apply_rate_limit and last_host_fetch_at is not None and host:
        last_host_fetch_at[host] = time.monotonic()

    if not html.ok or html.body is None:
        return SeedFetchResult(
            source_id=source.id,
            source_name=source.name,
            seed_url=url_normalized,
            status=STATUS_FAILED,
            http_status=html.status_code,
            final_url=html.final_url,
            message=html.error or "fetch failed",
        )

    return _persist_html_version(
        session,
        source=source,
        requested_url=url_normalized,
        url_normalized=url_normalized,
        discovery_status=discovery_status,
        html_body=html.body,
        content_type=html.content_type,
        http_status=html.status_code,
        final_url=html.final_url,
        storage_root=root,
    )


def fetch_approved_seed(
    session: Session,
    source_id: int,
    *,
    persist: bool = False,
    policy: CrawlPolicy | None = None,
    http_client=None,
    storage_root: Path | str | None = None,
    apply_rate_limit: bool = False,
    last_host_fetch_at: dict[str, float] | None = None,
) -> SeedFetchResult:
    """Fetch one approved source seed URL. Dry-run unless ``persist`` is True."""
    policy = policy or fetcher_policy()
    root = data_root(storage_root)
    source = _get_source(session, source_id)
    if source is None:
        return SeedFetchResult(
            source_id=source_id,
            source_name="",
            seed_url="",
            status=STATUS_FAILED,
            message="source not found",
        )

    # Fresh DB state for approval / patterns.
    session.refresh(source)
    plan_entry = evaluate_source(source, policy, client=http_client)
    if plan_entry.decision != DECISION_PLANNED:
        return SeedFetchResult(
            source_id=source.id,
            source_name=source.name,
            seed_url=source.seed_url,
            status=STATUS_SKIPPED,
            message=f"preflight {plan_entry.decision}: {plan_entry.notes}",
        )

    if not persist:
        return SeedFetchResult(
            source_id=source.id,
            source_name=source.name,
            seed_url=source.seed_url,
            status=STATUS_DRY_RUN,
            message=(
                "dry-run: would fetch seed HTML after preflight passed; "
                "pass --persist to download and store"
            ),
        )

    host = urlsplit(source.seed_url).hostname or ""
    if apply_rate_limit and last_host_fetch_at is not None and host:
        delay = plan_entry.crawl_delay_seconds or policy.default_crawl_delay_seconds
        last = last_host_fetch_at.get(host)
        if last is not None:
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)

    html = fetch_html_seed(
        source.seed_url,
        allowed_patterns=list(source.allowed_url_patterns or []),
        policy=policy,
        client=http_client,
    )
    if apply_rate_limit and last_host_fetch_at is not None and host:
        last_host_fetch_at[host] = time.monotonic()

    if not html.ok or html.body is None:
        return SeedFetchResult(
            source_id=source.id,
            source_name=source.name,
            seed_url=source.seed_url,
            status=STATUS_FAILED,
            http_status=html.status_code,
            final_url=html.final_url,
            message=html.error or "fetch failed",
        )

    return _persist_html_version(
        session,
        source=source,
        requested_url=source.seed_url,
        url_normalized=source.seed_url_normalized,
        discovery_status="seed",
        html_body=html.body,
        content_type=html.content_type,
        http_status=html.status_code,
        final_url=html.final_url,
        storage_root=root,
    )


def _content_type_or_html(value: str | None) -> str:
    return value or "text/html"


def fetch_approved_seeds(
    session: Session,
    *,
    persist: bool = False,
    source_id: int | None = None,
    policy: CrawlPolicy | None = None,
    http_client=None,
    storage_root: Path | str | None = None,
) -> SeedFetchReport:
    """Fetch one approved source or all approved sources."""
    policy = policy or fetcher_policy()
    report = SeedFetchReport()
    host_times: dict[str, float] = {}

    if source_id is not None:
        ids = [source_id]
    else:
        ids = list(
            session.scalars(
                select(Source.id)
                .where(Source.review_status == "approved")
                .order_by(Source.id)
            ).all()
        )

    for sid in ids:
        result = fetch_approved_seed(
            session,
            sid,
            persist=persist,
            policy=policy,
            http_client=http_client,
            storage_root=storage_root,
            apply_rate_limit=persist,
            last_host_fetch_at=host_times,
        )
        report.results.append(result)
    return report
