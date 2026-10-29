"""BFS frontier crawl loop for one approved source (Phase 6D.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from lifepilot.crawl.fetcher import (
    STATUS_BLOCKED_BY_BOT_PROTECTION,
    STATUS_FAILED,
    STATUS_FETCHED_NEW_VERSION,
    STATUS_INVALID_CONTENT,
    STATUS_SKIPPED,
    STATUS_UNCHANGED,
    SeedFetchResult,
    commit_fetch_result,
    fetch_binary_document_url,
    fetch_html_document_url,
)
from lifepilot.crawl.frontier import (
    claim_pending,
    enqueue_seed,
    enqueue_url,
    mark_blocked,
    mark_fetched,
    mark_permanent_failure,
    mark_skipped,
    mark_transient_failure,
)
from lifepilot.crawl.links import discover_links_from_html
from lifepilot.crawl.policy import CrawlPolicy
from lifepilot.crawl.sitemap import discover_sitemaps_for_source
from lifepilot.crawl.storage import data_root, safe_read_bytes
from lifepilot.db.current_version import get_current_version
from lifepilot.db.models import DiscoveryFrontier, Document, Source


@dataclass
class FrontierCrawlReport:
    source_id: int
    seed_enqueued: bool = False
    sitemap_fetches_attempted: int = 0
    sitemap_fetches_ok: int = 0
    sitemap_urls_enqueued_pending: int = 0
    document_fetches_attempted: int = 0
    html_fetches: int = 0
    binary_fetches: int = 0
    fetched_ok: int = 0
    unchanged: int = 0
    transient_retries: int = 0
    permanent_failures: int = 0
    blocked: int = 0
    skipped: int = 0
    children_enqueued: int = 0
    depth_capped_expansions: int = 0
    enqueue_cap_hits: int = 0
    fetch_results: list[SeedFetchResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stopped_reason: str | None = None


def classify_fetch_failure(
    result: SeedFetchResult,
) -> tuple[str, str]:
    """Return ``(class, frontier_status_hint)`` for a failed/blocked fetch.

    class: ``transient`` | ``permanent`` | ``blocked`` | ``skipped``
    """
    status = result.status
    msg = (result.message or "").lower()
    http = result.http_status

    if status == STATUS_BLOCKED_BY_BOT_PROTECTION:
        return "blocked", "blocked"
    if status == STATUS_INVALID_CONTENT:
        return "permanent", "failed"
    if status == STATUS_SKIPPED:
        return "skipped", "skipped"

    if http in {429, 503, 502, 504}:
        return "transient", "pending"
    if http is not None and 500 <= http <= 599:
        return "transient", "pending"
    if "timeout" in msg or "timed out" in msg:
        return "transient", "pending"
    if any(
        token in msg
        for token in (
            "connecterror",
            "networkerror",
            "remoteprotocolerror",
            "readerror",
            "connection",
        )
    ):
        return "transient", "pending"
    if "robots.txt unavailable" in msg:
        return "transient", "pending"
    if "robots.txt disallows" in msg:
        return "blocked", "blocked"
    if "outside approved allowlist" in msg:
        return "permanent", "out_of_scope"
    if http in {404, 410, 451}:
        return "permanent", "failed"
    if http in {401, 403}:
        return "permanent", "blocked"
    if status == STATUS_FAILED:
        return "permanent", "failed"
    return "permanent", "failed"


def _expand_html_children(
    session: Session,
    source: Source,
    *,
    document: Document,
    parent_depth: int,
    parent_url: str,
    policy: CrawlPolicy,
    enqueue_budget: list[int],
    report: FrontierCrawlReport,
    storage_root: Path,
) -> None:
    """Enqueue child links from stored HTML even when content is unchanged."""
    if parent_depth >= policy.max_depth:
        report.depth_capped_expansions += 1
        return
    current = get_current_version(session, document.id)
    if current is None:
        return
    try:
        raw = safe_read_bytes(storage_root, current.storage_path)
    except (OSError, ValueError) as exc:
        report.notes.append(f"expand read failed doc={document.id}: {exc}")
        return
    html = raw.decode("utf-8", errors="replace")
    links = discover_links_from_html(
        html,
        base_url=document.canonical_url,
        allowed_patterns=list(source.allowed_url_patterns or []),
    )
    child_depth = parent_depth + 1
    for link in links:
        if enqueue_budget[0] <= 0:
            report.enqueue_cap_hits += 1
            break
        if not link.url_normalized:
            continue
        if link.kind == "out_of_scope":
            outcome = enqueue_url(
                session,
                source,
                url=link.url_normalized,
                discovery_method="html_link",
                depth=child_depth,
                parent_url=parent_url,
            )
            # out_of_scope insert does not consume enqueue budget
            continue
        if link.kind not in {
            "in_scope_html",
            "in_scope_pdf",
            "in_scope_docx",
        }:
            continue
        hint = {
            "in_scope_html": "html_page",
            "in_scope_pdf": "pdf",
            "in_scope_docx": "docx",
        }[link.kind]
        result = enqueue_url(
            session,
            source,
            url=link.url_normalized,
            discovery_method="html_link",
            depth=child_depth,
            parent_url=parent_url,
            content_kind_hint=hint,
        )
        if result.outcome == "inserted_pending":
            report.children_enqueued += 1
            enqueue_budget[0] = max(0, enqueue_budget[0] - 1)


def crawl_source_frontier(
    session: Session,
    source: Source,
    *,
    policy: CrawlPolicy,
    http_client,
    storage_root: Path | str | None,
    network_budget: list[int],
    max_html: int,
    max_binary: int,
    host_times: dict[str, float],
    enable_sitemaps: bool = True,
) -> FrontierCrawlReport:
    """Enqueue seed/sitemap, then BFS-fetch pending frontier URLs under caps."""
    root = data_root(storage_root)
    report = FrontierCrawlReport(source_id=source.id)
    enqueue_budget = [policy.max_frontier_enqueues_per_source]

    seed_result = enqueue_seed(session, source)
    report.seed_enqueued = seed_result.outcome in {
        "inserted_pending",
        "unchanged",
    }
    if seed_result.outcome == "inserted_pending":
        enqueue_budget[0] = max(0, enqueue_budget[0] - 1)
    session.flush()

    if enable_sitemaps and network_budget[0] > 0:
        sm = discover_sitemaps_for_source(
            session,
            source,
            policy=policy,
            http_client=http_client,
            network_budget=network_budget,
            enqueue_budget=enqueue_budget,
        )
        report.sitemap_fetches_attempted = sm.sitemap_fetches_attempted
        report.sitemap_fetches_ok = sm.sitemap_fetches_ok
        report.sitemap_urls_enqueued_pending = sm.urls_enqueued_pending
        report.notes.extend(sm.notes[:10])
        session.flush()

    html_used = 0
    binary_used = 0

    while True:
        if network_budget[0] <= 0:
            report.stopped_reason = "network_budget"
            break
        remaining_doc_slots = (max_html - html_used) + (max_binary - binary_used)
        if remaining_doc_slots <= 0:
            report.stopped_reason = "document_caps"
            break

        batch = claim_pending(
            session,
            source.id,
            limit=min(5, network_budget[0], remaining_doc_slots),
            max_depth=policy.max_depth,
        )
        if not batch:
            report.stopped_reason = report.stopped_reason or "frontier_exhausted"
            break

        progress = False
        for row in batch:
            if network_budget[0] <= 0:
                report.stopped_reason = "network_budget"
                break
            kind = (row.content_kind_hint or "html_page").lower()
            if kind in {"pdf", "docx"}:
                if binary_used >= max_binary:
                    continue
            else:
                if html_used >= max_html:
                    continue

            progress = True
            result = _fetch_frontier_row(
                session,
                source,
                row,
                policy=policy,
                http_client=http_client,
                storage_root=root,
                host_times=host_times,
            )
            result = commit_fetch_result(session, result, storage_root=root)
            report.fetch_results.append(result)
            report.document_fetches_attempted += 1
            network_budget[0] = max(0, network_budget[0] - 1)
            if kind in {"pdf", "docx"}:
                binary_used += 1
                report.binary_fetches += 1
            else:
                html_used += 1
                report.html_fetches += 1

            _apply_fetch_to_frontier(
                session,
                source,
                row,
                result,
                policy=policy,
                enqueue_budget=enqueue_budget,
                report=report,
                storage_root=root,
            )
            session.flush()

        if not progress:
            # Pending rows exist but caps block them (e.g. only binaries left).
            report.stopped_reason = report.stopped_reason or "document_caps"
            break

    return report


def _fetch_frontier_row(
    session: Session,
    source: Source,
    row: DiscoveryFrontier,
    *,
    policy: CrawlPolicy,
    http_client,
    storage_root: Path,
    host_times: dict[str, float],
) -> SeedFetchResult:
    kind = (row.content_kind_hint or "html_page").lower()
    discovery_status = "seed" if row.discovery_method == "seed" else "discovered"
    if kind in {"pdf", "docx"}:
        return fetch_binary_document_url(
            session,
            source,
            url=row.url_normalized,
            expected_kind=kind,
            discovery_status=discovery_status,
            persist=True,
            policy=policy,
            http_client=http_client,
            storage_root=storage_root,
            apply_rate_limit=True,
            last_host_fetch_at=host_times,
        )
    return fetch_html_document_url(
        session,
        source,
        url=row.url_normalized,
        discovery_status=discovery_status,
        persist=True,
        policy=policy,
        http_client=http_client,
        storage_root=storage_root,
        apply_rate_limit=True,
        last_host_fetch_at=host_times,
    )


def _apply_fetch_to_frontier(
    session: Session,
    source: Source,
    row: DiscoveryFrontier,
    result: SeedFetchResult,
    *,
    policy: CrawlPolicy,
    enqueue_budget: list[int],
    report: FrontierCrawlReport,
    storage_root: Path,
) -> None:
    if result.status in {STATUS_FETCHED_NEW_VERSION, STATUS_UNCHANGED}:
        mark_fetched(session, row, document_id=result.document_id)
        if result.status == STATUS_FETCHED_NEW_VERSION:
            report.fetched_ok += 1
        else:
            report.unchanged += 1
        if result.document_id is not None:
            document = session.get(Document, result.document_id)
            if document is not None and document.content_kind == "html_page":
                _expand_html_children(
                    session,
                    source,
                    document=document,
                    parent_depth=row.depth,
                    parent_url=row.url_normalized,
                    policy=policy,
                    enqueue_budget=enqueue_budget,
                    report=report,
                    storage_root=storage_root,
                )
        return

    failure_class, _hint = classify_fetch_failure(result)
    reason = result.message or result.status
    if failure_class == "transient":
        outcome = mark_transient_failure(
            session,
            row,
            reason=reason,
            max_attempts=policy.max_frontier_attempts,
            base_backoff_seconds=policy.frontier_backoff_seconds,
            retry_after_seconds=result.retry_after_seconds,
        )
        if outcome == "pending":
            report.transient_retries += 1
        else:
            report.permanent_failures += 1
        return
    if failure_class == "blocked":
        mark_blocked(
            session, row, reason=reason, document_id=result.document_id
        )
        report.blocked += 1
        return
    if failure_class == "skipped":
        mark_skipped(
            session, row, reason=reason, document_id=result.document_id
        )
        report.skipped += 1
        return
    # permanent / out_of_scope-like
    if "outside approved allowlist" in reason.lower():
        row.status = "out_of_scope"
        row.last_error = reason
        row.failure_class = "permanent"
        row.next_attempt_at = None
        row.attempt_count = int(row.attempt_count or 0) + 1
        session.flush()
        report.permanent_failures += 1
        return
    mark_permanent_failure(
        session, row, reason=reason, document_id=result.document_id
    )
    report.permanent_failures += 1


__all__ = [
    "FrontierCrawlReport",
    "classify_fetch_failure",
    "crawl_source_frontier",
]
