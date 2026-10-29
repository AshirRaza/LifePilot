"""Bounded, resumable ingestion: fetch → discover → extract → compare.

Modes:
- ``full`` (default): approved-source fetch/discovery plus extract/compare.
- ``process-existing``: extract/compare stored raw versions only (zero HTTP).

Dry-run (default) performs no network I/O and no writes. Execute requires
``execute=True``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from lifepilot.crawl.frontier import (
    count_frontier,
    count_pending_eligible,
)
from lifepilot.crawl.frontier_crawl import crawl_source_frontier
from lifepilot.crawl.fetcher import (
    STATUS_BLOCKED_BY_BOT_PROTECTION,
    STATUS_FAILED,
    STATUS_FETCHED_NEW_VERSION,
    STATUS_INVALID_CONTENT,
    STATUS_SKIPPED,
    STATUS_UNCHANGED,
    SeedFetchResult,
)
from lifepilot.crawl.planner import DECISION_PLANNED, evaluate_source
from lifepilot.crawl.policy import CrawlPolicy, fetcher_policy
from lifepilot.crawl.source_docs import document_for_source_seed
from lifepilot.crawl.storage import data_root, relative_storage_path, write_bytes_atomic
from lifepilot.db.models import (
    Document,
    DocumentExtraction,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.extract.compare import (
    STATUS_CHANGED as CMP_CHANGED,
)
from lifepilot.extract.compare import (
    STATUS_UNAVAILABLE as CMP_UNAVAILABLE,
)
from lifepilot.extract.compare import (
    STATUS_UNCHANGED as CMP_UNCHANGED,
)
from lifepilot.extract.compare_runner import (
    compare_document_latest,
    latest_two_versions,
    persist_comparison,
)
from lifepilot.crawl.storage import data_root as _data_root_fn
from lifepilot.crawl.storage import safe_read_bytes
from lifepilot.extract.runner import (
    EXTRACTORS,
    STATUS_EMPTY,
    STATUS_FAILED as EXT_FAILED,
)
from lifepilot.extract.runner import (
    STATUS_SKIPPED as EXT_SKIPPED,
)
from lifepilot.extract.runner import (
    STATUS_SUCCESS,
    STATUS_UNCHANGED as EXT_UNCHANGED,
    extract_document_version,
    schema_version_from_extraction_bytes,
)
from lifepilot.extract.schema import SCHEMA_VERSION

MODE_FULL = "full"
MODE_PROCESS_EXISTING = "process-existing"
IngestMode = Literal["full", "process-existing"]


def _is_network_fetch_attempt(result: SeedFetchResult) -> bool:
    """True when the result represents a real document-fetch attempt."""
    if result.status == STATUS_SKIPPED and "skipped parent" in (result.message or ""):
        return False
    return True


@dataclass(frozen=True)
class IngestLimits:
    """Conservative global and per-source caps for one ingestion run.

    ``max_fetches`` is the shared **network** budget (document fetches +
    sitemap HTTP). HTML/PDF/DOCX per-source caps apply only to document
    fetches, not sitemap downloads.
    """

    max_sources: int = 1
    max_fetches: int = 5
    max_html_per_source: int = 3
    max_binary_per_source: int = 2
    max_extractions: int = 10
    max_comparisons: int = 10
    max_depth: int = 3


@dataclass
class StageFailure:
    stage: str
    reason: str
    source_id: int | None = None
    document_id: int | None = None
    version_id: int | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceRunStats:
    source_id: int
    name: str
    seed_url: str = ""
    review_status: str = ""
    status: str = "selected"  # selected|excluded|ok|failed|budget_exhausted
    fetches_attempted: int = 0
    fetches_new_version: int = 0
    fetches_unchanged: int = 0
    fetches_failed: int = 0
    fetches_blocked: int = 0
    extractions_success: int = 0
    extractions_unchanged: int = 0
    extractions_failed: int = 0
    extractions_skipped: int = 0
    extractions_empty: int = 0
    comparisons_unchanged: int = 0
    comparisons_changed: int = 0
    comparisons_unavailable: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IngestRunReport:
    started_at: str
    ended_at: str | None = None
    mode: str = MODE_FULL
    execute: bool = False
    completed: bool = False
    partial: bool = False
    stopped_due_to_limits: bool = False
    limits: dict[str, int] = field(default_factory=dict)
    selected_sources: list[dict[str, Any]] = field(default_factory=list)
    per_source: list[SourceRunStats] = field(default_factory=list)
    planned: dict[str, Any] = field(default_factory=dict)
    fetches_attempted: int = 0
    fetches_new_version: int = 0
    fetches_unchanged: int = 0
    fetches_skipped: int = 0
    fetches_failed: int = 0
    fetches_blocked: int = 0
    extractions_success: int = 0
    extractions_unchanged: int = 0
    extractions_skipped: int = 0
    extractions_failed: int = 0
    extractions_empty: int = 0
    comparisons_unchanged: int = 0
    comparisons_changed: int = 0
    comparisons_unavailable: int = 0
    extraction_backlog_remaining: int | None = None
    comparison_backlog_remaining: int | None = None
    frontier_pending_remaining: int | None = None
    frontier_fetched: int = 0
    frontier_skipped: int = 0
    frontier_blocked: int = 0
    frontier_failed: int = 0
    frontier_out_of_scope: int = 0
    sitemap_fetches_attempted: int = 0
    sitemap_fetches_ok: int = 0
    sitemap_urls_enqueued: int = 0
    frontier_children_enqueued: int = 0
    frontier_transient_retries: int = 0
    depth_capped_expansions: int = 0
    coverage_status: str = "unknown"
    # True only when eligible frontier is empty, extraction backlog is empty,
    # and discovery was not cut short by depth/budget caps.
    complete_within_approved_scope: bool = False
    limits_reached: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    failures: list[StageFailure] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    attempted_urls: list[dict[str, Any]] = field(default_factory=list)
    report_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["failures"] = [
            f.to_dict() if hasattr(f, "to_dict") else f for f in self.failures
        ]
        data["per_source"] = [
            s.to_dict() if hasattr(s, "to_dict") else s for s in self.per_source
        ]
        return data


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _source_stats_map(report: IngestRunReport) -> dict[int, SourceRunStats]:
    return {s.source_id: s for s in report.per_source}


def _normalize_country_code(country: str | None) -> str | None:
    if country is None:
        return None
    code = country.strip().upper()
    if len(code) < 2:
        raise ValueError(f"invalid country code: {country!r}")
    return code


def _select_sources(
    session: Session,
    *,
    source_id: int | None = None,
    source_ids: list[int] | None = None,
    max_sources: int,
    all_approved: bool = False,
    country: str | None = None,
) -> tuple[list[Source], list[str]]:
    exclusions: list[str] = []
    try:
        country_code = _normalize_country_code(country)
    except ValueError as exc:
        exclusions.append(str(exc))
        return [], exclusions

    explicit_ids: list[int] = []
    if source_ids:
        explicit_ids.extend(int(i) for i in source_ids)
    if source_id is not None:
        explicit_ids.append(int(source_id))
    # Deterministic unique order preserving first occurrence.
    seen_ids: set[int] = set()
    ordered_ids: list[int] = []
    for sid in explicit_ids:
        if sid not in seen_ids:
            seen_ids.add(sid)
            ordered_ids.append(sid)

    if ordered_ids and all_approved:
        exclusions.append("cannot combine --source-id with --all-approved")
        return [], exclusions
    if ordered_ids and country_code is not None:
        exclusions.append("cannot combine --source-id with --country")
        return [], exclusions

    if ordered_ids:
        selected: list[Source] = []
        for sid in ordered_ids:
            source = session.get(Source, sid)
            if source is None:
                exclusions.append(f"source_id={sid} not found")
                continue
            if source.review_status != "approved":
                exclusions.append(
                    f"source_id={sid} review_status={source.review_status!r}; "
                    "not approved"
                )
                continue
            selected.append(source)
        return selected, exclusions

    stmt = (
        select(Source)
        .join(Jurisdiction, Jurisdiction.id == Source.jurisdiction_id)
        .where(Source.review_status == "approved")
        .order_by(Source.id.asc())
    )
    if country_code is not None:
        # Match country jurisdictions by ISO code. Supranational codes (e.g. EU)
        # match Jurisdiction.code only — never expand EU into member states.
        stmt = stmt.where(
            or_(
                Jurisdiction.country_code == country_code,
                Jurisdiction.code == country_code,
            )
        )

    if not all_approved:
        stmt = stmt.limit(max_sources)
    sources = list(session.scalars(stmt).all())
    if country_code is not None and not sources:
        exclusions.append(
            f"no approved sources for country/jurisdiction {country_code!r}"
        )
    return sources, exclusions


def _needs_extraction(
    session: Session,
    version: DocumentVersion,
    document: Document,
    *,
    storage_root: Path | str | None = None,
) -> bool:
    """Return True when a version should be (re)extracted.

    Retry policy:
    - Missing row → extract.
    - Extractor version upgrade → extract (including prior empty results).
    - ``failed`` → recoverable; keep retrying.
    - ``empty`` at current extractor version → terminal (do not reprocess).
    - ``skipped`` at current extractor version → terminal.
    - ``success`` → re-extract only when storage/schema is missing or stale.
    """
    spec = EXTRACTORS.get(document.content_kind)
    if spec is None:
        return False
    if document.discovery_status == "rejected":
        return False
    row = session.scalar(
        select(DocumentExtraction).where(
            DocumentExtraction.document_version_id == version.id,
            DocumentExtraction.extractor_name == spec.name,
        )
    )
    if row is None:
        return True
    if row.extractor_version != spec.version:
        return True
    if row.status == EXT_FAILED:
        return True
    if row.status == STATUS_EMPTY:
        return False
    if row.status == EXT_SKIPPED:
        return False
    if row.status != STATUS_SUCCESS:
        return True
    if not row.storage_path or not row.content_sha256:
        return True
    try:
        raw = safe_read_bytes(_data_root_fn(storage_root), row.storage_path)
        schema = schema_version_from_extraction_bytes(raw)
        if schema != SCHEMA_VERSION:
            return True
    except (OSError, FileNotFoundError, ValueError):
        return True
    return False


def _eligible_document_ids(session: Session, source_ids: list[int]) -> list[int]:
    """Documents owned by or fetched via the selected approved sources."""
    if not source_ids:
        return []
    owned = set(
        session.scalars(
            select(Document.id).where(Document.primary_source_id.in_(source_ids))
        ).all()
    )
    via = set(
        session.scalars(
            select(DocumentVersion.document_id)
            .where(DocumentVersion.fetched_via_source_id.in_(source_ids))
            .distinct()
        ).all()
    )
    return sorted(owned | via)


def _versions_needing_extraction(
    session: Session,
    document_ids: list[int],
    *,
    limit: int | None = None,
    storage_root: Path | str | None = None,
) -> list[tuple[Document, DocumentVersion]]:
    if not document_ids:
        return []
    if limit is not None and limit < 1:
        return []
    pairs = list(
        session.execute(
            select(Document, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(Document.id.in_(document_ids))
            .order_by(Document.id.asc(), DocumentVersion.version_number.asc())
        ).all()
    )
    needed: list[tuple[Document, DocumentVersion]] = []
    for document, version in pairs:
        if _needs_extraction(
            session, version, document, storage_root=storage_root
        ):
            needed.append((document, version))
            if limit is not None and len(needed) >= limit:
                break
    return needed


def _count_extraction_backlog(
    session: Session,
    document_ids: list[int],
    *,
    storage_root: Path | str | None = None,
) -> int:
    return len(
        _versions_needing_extraction(
            session, document_ids, limit=None, storage_root=storage_root
        )
    )


def _documents_comparable(
    session: Session, document_ids: list[int], *, limit: int | None = None
) -> list[int]:
    if not document_ids:
        return []
    if limit is not None and limit < 1:
        return []
    out: list[int] = []
    for did in document_ids:
        if latest_two_versions(session, did) is not None:
            out.append(did)
            if limit is not None and len(out) >= limit:
                break
    return out


def _count_comparison_backlog(session: Session, document_ids: list[int]) -> int:
    """Documents with ≥2 versions (may still be unavailable without extractions)."""
    return len(_documents_comparable(session, document_ids, limit=None))


def _policy_for_remaining(
    base: CrawlPolicy,
    *,
    remaining_fetches: int,
    limits: IngestLimits,
) -> CrawlPolicy:
    html_cap = max(0, min(limits.max_html_per_source, remaining_fetches))
    binary_cap = max(0, min(limits.max_binary_per_source, remaining_fetches))
    return fetcher_policy(
        default_crawl_delay_seconds=base.default_crawl_delay_seconds,
        min_crawl_delay_seconds=base.min_crawl_delay_seconds,
        max_requests_per_host_per_minute=base.max_requests_per_host_per_minute,
        require_robots_ok=base.require_robots_ok,
        request_timeout_seconds=base.request_timeout_seconds,
        max_response_bytes=base.max_response_bytes,
        max_redirects=base.max_redirects,
        max_discovered_links_listed=base.max_discovered_links_listed,
        max_discovered_html_fetches_per_run=html_cap,
        max_discovered_binary_fetches_per_run=binary_cap,
        max_binary_response_bytes=base.max_binary_response_bytes,
        max_docx_uncompressed_bytes=base.max_docx_uncompressed_bytes,
        binary_request_timeout_seconds=base.binary_request_timeout_seconds,
        max_depth=limits.max_depth,
        max_frontier_enqueues_per_source=base.max_frontier_enqueues_per_source,
        max_frontier_attempts=base.max_frontier_attempts,
        frontier_backoff_seconds=base.frontier_backoff_seconds,
        max_sitemap_fetches_per_source=base.max_sitemap_fetches_per_source,
        max_sitemap_urls_enqueued_per_run=base.max_sitemap_urls_enqueued_per_run,
        max_sitemap_response_bytes=base.max_sitemap_response_bytes,
        max_sitemap_index_depth=base.max_sitemap_index_depth,
    )


def _record_fetch(
    report: IngestRunReport,
    result: SeedFetchResult,
    *,
    stats: SourceRunStats | None = None,
) -> None:
    if _is_network_fetch_attempt(result):
        report.fetches_attempted += 1
        if stats is not None:
            stats.fetches_attempted += 1
        report.attempted_urls.append(
            {
                "url": result.seed_url,
                "status": result.status,
                "http_status": result.http_status,
                "document_id": result.document_id,
                "version_id": result.version_id,
                "source_id": result.source_id,
                "message": result.message,
            }
        )
    if result.status == STATUS_FETCHED_NEW_VERSION:
        report.fetches_new_version += 1
        if stats is not None:
            stats.fetches_new_version += 1
    elif result.status == STATUS_UNCHANGED:
        report.fetches_unchanged += 1
        if stats is not None:
            stats.fetches_unchanged += 1
    elif result.status == STATUS_SKIPPED:
        report.fetches_skipped += 1
    elif result.status in {
        STATUS_BLOCKED_BY_BOT_PROTECTION,
        STATUS_INVALID_CONTENT,
    }:
        report.fetches_blocked += 1
        if stats is not None:
            stats.fetches_blocked += 1
        report.failures.append(
            StageFailure(
                stage="fetch",
                reason=result.message or result.status,
                source_id=result.source_id,
                document_id=result.document_id,
                url=result.seed_url,
            )
        )
    elif result.status == STATUS_FAILED:
        report.fetches_failed += 1
        if stats is not None:
            stats.fetches_failed += 1
        report.failures.append(
            StageFailure(
                stage="fetch",
                reason=result.message or result.status,
                source_id=result.source_id,
                document_id=result.document_id,
                url=result.seed_url,
            )
        )


def _init_per_source(report: IngestRunReport, sources: list[Source]) -> None:
    report.per_source = [
        SourceRunStats(
            source_id=s.id,
            name=s.name,
            seed_url=s.seed_url or "",
            review_status=s.review_status,
            status="selected",
        )
        for s in sources
    ]
    report.selected_sources = [
        {
            "source_id": s.source_id,
            "name": s.name,
            "seed_url": s.seed_url,
            "review_status": s.review_status,
        }
        for s in report.per_source
    ]


def _dry_run(
    session: Session,
    *,
    sources: list[Source],
    limits: IngestLimits,
    report: IngestRunReport,
    mode: str,
) -> IngestRunReport:
    report.notes.append(
        "dry-run: no network I/O, no filesystem writes, no DB mutations"
    )
    if mode == MODE_FULL:
        report.notes.append(
            "robots.txt checks occur only on --execute (full mode) and are "
            "outside the document-fetch budget"
        )
    else:
        report.notes.append(
            "process-existing: zero HTTP including robots.txt; "
            "operates on stored raw versions only"
        )

    _init_per_source(report, sources)
    source_ids = [s.id for s in sources]
    for source in sources:
        if not source.seed_url or not (source.allowed_url_patterns or []):
            report.exclusions.append(
                f"source_id={source.id} missing seed URL or allowlist patterns"
            )

    doc_ids = _eligible_document_ids(session, source_ids)
    # Dry-run uses the default data root for schema_version file checks.
    backlog = _count_extraction_backlog(session, doc_ids)
    pending_extract = _versions_needing_extraction(
        session, doc_ids, limit=limits.max_extractions
    )
    comparable = _documents_comparable(
        session, doc_ids, limit=limits.max_comparisons
    )
    cmp_backlog = _count_comparison_backlog(session, doc_ids)
    seed_docs = sum(
        1 for s in sources if document_for_source_seed(session, s) is not None
    )
    report.extraction_backlog_remaining = backlog
    report.comparison_backlog_remaining = cmp_backlog
    report.planned = {
        "mode": mode,
        "sources": len(sources),
        "eligible_documents": len(doc_ids),
        "would_fetch_seeds": 0 if mode == MODE_PROCESS_EXISTING else len(sources),
        "would_discover_html": (
            "n/a (process-existing)"
            if mode == MODE_PROCESS_EXISTING
            else "unknown_without_fetch (bounded by max_html_per_source / max_fetches)"
        ),
        "would_discover_binary": (
            "n/a (process-existing)"
            if mode == MODE_PROCESS_EXISTING
            else "unknown_without_fetch (bounded by max_binary_per_source / max_fetches)"
        ),
        "existing_seed_documents": seed_docs,
        "versions_needing_extraction_this_run": len(pending_extract),
        "extraction_backlog_total": backlog,
        "documents_with_consecutive_versions_this_run": len(comparable),
        "comparison_backlog_total": cmp_backlog,
        "fetch_budget": 0 if mode == MODE_PROCESS_EXISTING else limits.max_fetches,
        "extraction_budget": limits.max_extractions,
        "comparison_budget": limits.max_comparisons,
    }
    report.completed = True
    report.ended_at = _iso(_utcnow())
    return report


def _execute_source(
    session: Session,
    source: Source,
    *,
    report: IngestRunReport,
    limits: IngestLimits,
    policy: CrawlPolicy,
    http_client,
    storage_root: Path,
    fetch_budget: list[int],
    host_times: dict[str, float],
    touched_document_ids: set[int],
) -> None:
    """Frontier BFS crawl for one approved source (seed + sitemap + links)."""
    stats = _source_stats_map(report).get(source.id)
    remaining = fetch_budget[0]
    if remaining <= 0:
        if "max_fetches" not in report.limits_reached:
            report.limits_reached.append("max_fetches")
        if stats is not None:
            stats.status = "budget_exhausted"
            stats.notes.append("global network budget exhausted before this source")
        return

    # Robots/preflight may use HTTP but is outside the shared network budget.
    plan_entry = evaluate_source(source, policy, client=http_client)
    if plan_entry.decision != DECISION_PLANNED:
        report.exclusions.append(
            f"source_id={source.id} preflight {plan_entry.decision}: {plan_entry.notes}"
        )
        report.fetches_skipped += 1
        if stats is not None:
            stats.status = "excluded"
            stats.notes.append(f"preflight {plan_entry.decision}: {plan_entry.notes}")
        return

    scoped = _policy_for_remaining(policy, remaining_fetches=remaining, limits=limits)
    try:
        crawl = crawl_source_frontier(
            session,
            source,
            policy=scoped,
            http_client=http_client,
            storage_root=storage_root,
            network_budget=fetch_budget,
            max_html=limits.max_html_per_source,
            max_binary=limits.max_binary_per_source,
            host_times=host_times,
            enable_sitemaps=True,
        )
    except Exception as exc:  # noqa: BLE001 — isolate per source
        session.rollback()
        report.failures.append(
            StageFailure(
                stage="frontier_crawl",
                reason=str(exc),
                source_id=source.id,
                url=source.seed_url,
            )
        )
        report.fetches_failed += 1
        if stats is not None:
            stats.status = "failed"
            stats.fetches_failed += 1
            stats.notes.append(str(exc))
        return

    report.sitemap_fetches_attempted += crawl.sitemap_fetches_attempted
    report.sitemap_fetches_ok += crawl.sitemap_fetches_ok
    report.sitemap_urls_enqueued += crawl.sitemap_urls_enqueued_pending
    report.frontier_children_enqueued += crawl.children_enqueued
    report.frontier_transient_retries += crawl.transient_retries
    report.depth_capped_expansions += crawl.depth_capped_expansions

    for item in crawl.fetch_results:
        _record_fetch(report, item, stats=stats)
        if item.document_id is not None:
            touched_document_ids.add(item.document_id)

    if crawl.stopped_reason == "network_budget":
        if "max_fetches" not in report.limits_reached:
            report.limits_reached.append("max_fetches")
    elif crawl.stopped_reason == "document_caps":
        if "document_fetch_caps" not in report.limits_reached:
            report.limits_reached.append("document_fetch_caps")
    if crawl.depth_capped_expansions:
        if "max_depth" not in report.limits_reached:
            report.limits_reached.append("max_depth")
    if crawl.enqueue_cap_hits:
        if "max_frontier_enqueues" not in report.limits_reached:
            report.limits_reached.append("max_frontier_enqueues")

    if stats is not None:
        for note in crawl.notes[:5]:
            stats.notes.append(note)
        if crawl.stopped_reason:
            stats.notes.append(f"frontier_stopped={crawl.stopped_reason}")
        if stats.status == "selected":
            stats.status = "ok"


def _execute_extractions(
    session: Session,
    *,
    document_ids: list[int],
    report: IngestRunReport,
    limits: IngestLimits,
    storage_root: Path,
    touched_document_ids: set[int],
    processed_version_ids: set[int],
) -> None:
    needed = _versions_needing_extraction(
        session,
        document_ids,
        limit=limits.max_extractions,
        storage_root=storage_root,
    )
    total_backlog = _count_extraction_backlog(
        session, document_ids, storage_root=storage_root
    )
    if total_backlog > limits.max_extractions:
        if "max_extractions" not in report.limits_reached:
            report.limits_reached.append("max_extractions")

    stats_by_source = _source_stats_map(report)

    for document, version in needed:
        if version.id in processed_version_ids:
            continue
        processed_version_ids.add(version.id)
        stats = stats_by_source.get(document.primary_source_id)
        try:
            result = extract_document_version(
                session,
                version,
                document,
                persist=True,
                storage_root=storage_root,
            )
            session.commit()
            touched_document_ids.add(document.id)
            if result.status == STATUS_SUCCESS:
                report.extractions_success += 1
                if stats is not None:
                    stats.extractions_success += 1
            elif result.status == EXT_UNCHANGED:
                report.extractions_unchanged += 1
                if stats is not None:
                    stats.extractions_unchanged += 1
            elif result.status == EXT_SKIPPED:
                report.extractions_skipped += 1
                if stats is not None:
                    stats.extractions_skipped += 1
            elif result.status == STATUS_EMPTY:
                report.extractions_empty += 1
                if stats is not None:
                    stats.extractions_empty += 1
            elif result.status == EXT_FAILED:
                report.extractions_failed += 1
                if stats is not None:
                    stats.extractions_failed += 1
                report.failures.append(
                    StageFailure(
                        stage="extract",
                        reason=result.message or result.status,
                        document_id=document.id,
                        version_id=version.id,
                        source_id=document.primary_source_id,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            report.extractions_failed += 1
            if stats is not None:
                stats.extractions_failed += 1
            report.failures.append(
                StageFailure(
                    stage="extract",
                    reason=str(exc),
                    document_id=document.id,
                    version_id=version.id,
                    source_id=document.primary_source_id,
                )
            )


def _execute_comparisons(
    session: Session,
    *,
    document_ids: list[int],
    report: IngestRunReport,
    limits: IngestLimits,
    storage_root: Path,
    processed_document_ids: set[int],
) -> None:
    count = 0
    stats_by_source = _source_stats_map(report)
    for did in document_ids:
        if did in processed_document_ids:
            continue
        if count >= limits.max_comparisons:
            if "max_comparisons" not in report.limits_reached:
                report.limits_reached.append("max_comparisons")
            break
        if latest_two_versions(session, did) is None:
            continue
        document = session.get(Document, did)
        stats = (
            stats_by_source.get(document.primary_source_id)
            if document is not None
            else None
        )
        try:
            result = compare_document_latest(
                session, did, storage_root=storage_root
            )
            result = persist_comparison(
                session, result, storage_root=storage_root
            )
            session.commit()
            processed_document_ids.add(did)
            count += 1
            if result.status == CMP_UNCHANGED:
                report.comparisons_unchanged += 1
                if stats is not None:
                    stats.comparisons_unchanged += 1
            elif result.status == CMP_CHANGED:
                report.comparisons_changed += 1
                if stats is not None:
                    stats.comparisons_changed += 1
            else:
                report.comparisons_unavailable += 1
                if stats is not None:
                    stats.comparisons_unavailable += 1
                if result.unavailable_reason:
                    report.notes.append(
                        f"document_id={did} compare unavailable: "
                        f"{result.unavailable_reason}"
                    )
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            report.failures.append(
                StageFailure(
                    stage="compare",
                    reason=str(exc),
                    document_id=did,
                    source_id=document.primary_source_id if document else None,
                )
            )


def _write_run_report(root: Path, report: IngestRunReport) -> str:
    runs = root / "runs"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = runs / f"ingest_{stamp}.json"
    body = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    write_bytes_atomic(path, (body + "\n").encode("utf-8"))
    return relative_storage_path(root, path)


def _finalize_backlogs(
    session: Session,
    report: IngestRunReport,
    document_ids: list[int],
    *,
    storage_root: Path | str | None = None,
    source_ids: list[int] | None = None,
    max_depth: int = 3,
) -> None:
    report.extraction_backlog_remaining = _count_extraction_backlog(
        session, document_ids, storage_root=storage_root
    )
    report.comparison_backlog_remaining = _count_comparison_backlog(
        session, document_ids
    )
    ids = source_ids or []
    if ids:
        report.frontier_pending_remaining = count_pending_eligible(
            session, ids, max_depth=max_depth
        )
        report.frontier_fetched = count_frontier(session, ids, status="fetched")
        report.frontier_skipped = count_frontier(session, ids, status="skipped")
        report.frontier_blocked = count_frontier(session, ids, status="blocked")
        report.frontier_failed = count_frontier(session, ids, status="failed")
        report.frontier_out_of_scope = count_frontier(
            session, ids, status="out_of_scope"
        )
    _set_coverage_status(report)


def _set_coverage_status(report: IngestRunReport) -> None:
    """Classify crawl completeness; never claim complete if work remains."""
    pending = int(report.frontier_pending_remaining or 0)
    extract_backlog = int(report.extraction_backlog_remaining or 0)
    limits = set(report.limits_reached)
    depth_limited = "max_depth" in limits or report.depth_capped_expansions > 0
    budget_limited = bool(
        limits
        & {
            "max_fetches",
            "document_fetch_caps",
            "max_frontier_enqueues",
            "max_extractions",
            "max_comparisons",
        }
    )

    if pending > 0 and budget_limited:
        report.coverage_status = "budget_limited"
    elif pending > 0 and depth_limited:
        report.coverage_status = "depth_limited"
    elif pending > 0:
        report.coverage_status = "frontier_pending"
    elif depth_limited and not budget_limited:
        # No pending within max_depth, but expansion was depth-capped.
        report.coverage_status = "depth_limited"
    elif extract_backlog > 0:
        report.coverage_status = "extraction_backlog_remaining"
    elif budget_limited:
        report.coverage_status = "budget_limited"
    else:
        report.coverage_status = "exhausted_within_scope"

    report.complete_within_approved_scope = (
        pending == 0
        and extract_backlog == 0
        and not depth_limited
        and not budget_limited
        and report.coverage_status == "exhausted_within_scope"
    )


def run_ingestion(
    session: Session,
    *,
    execute: bool = False,
    mode: IngestMode = MODE_FULL,
    source_id: int | None = None,
    source_ids: list[int] | None = None,
    all_approved: bool = False,
    country: str | None = None,
    limits: IngestLimits | None = None,
    policy: CrawlPolicy | None = None,
    http_client=None,
    storage_root: Path | str | None = None,
    write_report_file: bool = True,
) -> IngestRunReport:
    """Orchestrate a bounded ingestion run (dry-run by default)."""
    if mode not in {MODE_FULL, MODE_PROCESS_EXISTING}:
        raise ValueError(f"unsupported ingest mode: {mode!r}")
    limits = limits or IngestLimits()
    if limits.max_sources < 1:
        raise ValueError("max_sources must be >= 1")
    root = data_root(storage_root)
    policy = policy or fetcher_policy()
    started = _utcnow()
    report = IngestRunReport(
        started_at=_iso(started),
        mode=mode,
        execute=execute,
        limits={
            "max_sources": limits.max_sources,
            "max_fetches": limits.max_fetches,
            "max_html_per_source": limits.max_html_per_source,
            "max_binary_per_source": limits.max_binary_per_source,
            "max_extractions": limits.max_extractions,
            "max_comparisons": limits.max_comparisons,
            "max_depth": limits.max_depth,
        },
    )
    if country:
        report.notes.append(f"country_filter={country.strip().upper()}")

    sources, exclusions = _select_sources(
        session,
        source_id=source_id,
        source_ids=source_ids,
        max_sources=limits.max_sources,
        all_approved=all_approved,
        country=country,
    )
    report.exclusions.extend(exclusions)
    if not sources:
        report.notes.append("no approved sources selected")
        report.completed = True
        report.ended_at = _iso(_utcnow())
        return report

    total_approved = session.scalar(
        select(func.count())
        .select_from(Source)
        .where(Source.review_status == "approved")
    )
    explicit_selection = bool(source_id is not None or source_ids)
    if (
        not explicit_selection
        and total_approved
        and int(total_approved) > len(sources)
    ):
        report.limits_reached.append("max_sources")

    if not execute:
        return _dry_run(
            session, sources=sources, limits=limits, report=report, mode=mode
        )

    _init_per_source(report, sources)
    source_ids = [s.id for s in sources]
    doc_ids = _eligible_document_ids(session, source_ids)
    touched: set[int] = set()
    processed_versions: set[int] = set()
    processed_compare_docs: set[int] = set()

    if mode == MODE_FULL:
        report.notes.append(
            "network budget covers document fetches + sitemap HTTP; "
            "HTML/PDF/DOCX caps are separate; robots preflight is outside budget"
        )
        fetch_budget = [limits.max_fetches]
        host_times: dict[str, float] = {}
        # Align policy max_depth with limits for this run.
        policy = fetcher_policy(
            default_crawl_delay_seconds=policy.default_crawl_delay_seconds,
            min_crawl_delay_seconds=policy.min_crawl_delay_seconds,
            max_requests_per_host_per_minute=policy.max_requests_per_host_per_minute,
            require_robots_ok=policy.require_robots_ok,
            request_timeout_seconds=policy.request_timeout_seconds,
            max_response_bytes=policy.max_response_bytes,
            max_redirects=policy.max_redirects,
            max_discovered_links_listed=policy.max_discovered_links_listed,
            max_discovered_html_fetches_per_run=policy.max_discovered_html_fetches_per_run,
            max_discovered_binary_fetches_per_run=(
                policy.max_discovered_binary_fetches_per_run
            ),
            max_binary_response_bytes=policy.max_binary_response_bytes,
            max_docx_uncompressed_bytes=policy.max_docx_uncompressed_bytes,
            binary_request_timeout_seconds=policy.binary_request_timeout_seconds,
            max_depth=limits.max_depth,
            max_frontier_enqueues_per_source=policy.max_frontier_enqueues_per_source,
            max_frontier_attempts=policy.max_frontier_attempts,
            frontier_backoff_seconds=policy.frontier_backoff_seconds,
            max_sitemap_fetches_per_source=policy.max_sitemap_fetches_per_source,
            max_sitemap_urls_enqueued_per_run=policy.max_sitemap_urls_enqueued_per_run,
            max_sitemap_response_bytes=policy.max_sitemap_response_bytes,
            max_sitemap_index_depth=policy.max_sitemap_index_depth,
        )
        for source in sources:
            if fetch_budget[0] <= 0:
                if "max_fetches" not in report.limits_reached:
                    report.limits_reached.append("max_fetches")
                stats = _source_stats_map(report).get(source.id)
                if stats is not None and stats.status == "selected":
                    stats.status = "budget_exhausted"
                    stats.notes.append("skipped: global network budget exhausted")
                continue
            _execute_source(
                session,
                source,
                report=report,
                limits=limits,
                policy=policy,
                http_client=http_client,
                storage_root=root,
                fetch_budget=fetch_budget,
                host_times=host_times,
                touched_document_ids=touched,
            )
        # Refresh eligible docs after fetch (new discoveries).
        doc_ids = _eligible_document_ids(session, source_ids)
    else:
        report.notes.append(
            "process-existing mode: no HTTP requests; processing stored raw versions"
        )

    _execute_extractions(
        session,
        document_ids=doc_ids,
        report=report,
        limits=limits,
        storage_root=root,
        touched_document_ids=touched,
        processed_version_ids=processed_versions,
    )

    compare_ids: list[int] = []
    seen: set[int] = set()
    for did in sorted(touched) + _documents_comparable(
        session, doc_ids, limit=limits.max_comparisons
    ):
        if did not in seen:
            compare_ids.append(did)
            seen.add(did)

    _execute_comparisons(
        session,
        document_ids=compare_ids,
        report=report,
        limits=limits,
        storage_root=root,
        processed_document_ids=processed_compare_docs,
    )

    _finalize_backlogs(
        session,
        report,
        doc_ids,
        storage_root=root,
        source_ids=source_ids,
        max_depth=limits.max_depth,
    )
    report.stopped_due_to_limits = bool(report.limits_reached)
    report.partial = (
        bool(report.failures)
        or report.stopped_due_to_limits
        or not report.complete_within_approved_scope
    )
    report.completed = True
    report.ended_at = _iso(_utcnow())
    if write_report_file:
        try:
            report.report_path = _write_run_report(root, report)
        except OSError as exc:
            report.notes.append(f"could not write run report file: {exc}")
    return report
