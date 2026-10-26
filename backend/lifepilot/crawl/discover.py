"""Bounded link discovery from stored HTML document versions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.crawl.fetcher import (
    STATUS_DRY_RUN,
    STATUS_SKIPPED,
    SeedFetchResult,
    fetch_html_document_url,
)
from lifepilot.crawl.links import ExtractedLink, discover_links_from_html
from lifepilot.crawl.policy import CrawlPolicy, fetcher_policy
from lifepilot.crawl.storage import data_root
from lifepilot.db.current_version import get_current_version
from lifepilot.db.models import Document, Source


@dataclass
class DiscoveryReport:
    parent_document_id: int
    source_id: int
    base_url: str
    links: list[ExtractedLink] = field(default_factory=list)
    fetch_results: list[SeedFetchResult] = field(default_factory=list)
    message: str = ""

    @property
    def in_scope_html(self) -> list[ExtractedLink]:
        return [link for link in self.links if link.kind == "in_scope_html"]

    @property
    def in_scope_pdf(self) -> list[ExtractedLink]:
        return [link for link in self.links if link.kind == "in_scope_pdf"]

    @property
    def in_scope_docx(self) -> list[ExtractedLink]:
        return [link for link in self.links if link.kind == "in_scope_docx"]

    @property
    def out_of_scope(self) -> list[ExtractedLink]:
        return [link for link in self.links if link.kind == "out_of_scope"]


def _safe_read_stored_html(root: Path, storage_path: str) -> bytes:
    path = (root / storage_path).resolve()
    root_resolved = root.resolve()
    if not str(path).startswith(str(root_resolved)):
        raise ValueError(f"storage_path escapes data root: {storage_path}")
    if not path.is_file():
        raise FileNotFoundError(f"stored HTML not found: {path}")
    return path.read_bytes()


def _document_for_source_seed(session: Session, source: Source) -> Document | None:
    return session.scalar(
        select(Document).where(Document.url_normalized == source.seed_url_normalized)
    )


def _resolve_crawl_source(
    session: Session,
    document: Document,
    crawl_source_id: int | None,
) -> tuple[Source | None, str]:
    """Pick the approved source whose allowlist governs this discovery run.

    Crawl scope is always the explicitly selected source when provided.
    Document ``primary_source_id`` is ownership only and is used as a fallback
    for document-id discovery, not as an inherited broader allowlist.
    """
    if crawl_source_id is not None:
        source = session.get(Source, crawl_source_id)
        if source is None:
            return None, "crawl source not found"
        if source.review_status != "approved":
            return None, (
                f"crawl source review_status={source.review_status!r}; not approved"
            )
        return source, ""

    source = session.get(Source, document.primary_source_id)
    if source is None or source.review_status != "approved":
        return None, "document primary source missing or not approved"
    return source, ""


def discover_from_document(
    session: Session,
    document_id: int,
    *,
    crawl_source_id: int | None = None,
    persist: bool = False,
    policy: CrawlPolicy | None = None,
    http_client=None,
    storage_root: Path | str | None = None,
) -> DiscoveryReport:
    """Discover in-scope links from a stored document's current HTML version.

    ``crawl_source_id`` selects the approved source whose allowlist and identity
    govern filtering and fetches. When omitted, the document's
    ``primary_source_id`` is used. Ownership fields on the document are never
    rewritten by discovery.

    Dry-run lists candidates. ``persist`` fetches in-scope HTML only (not PDFs).
    """
    policy = policy or fetcher_policy()
    root = data_root(storage_root)
    document = session.get(Document, document_id)
    if document is None:
        return DiscoveryReport(
            parent_document_id=document_id,
            source_id=crawl_source_id or 0,
            base_url="",
            message="document not found",
        )

    source, source_error = _resolve_crawl_source(
        session, document, crawl_source_id
    )
    if source is None:
        return DiscoveryReport(
            parent_document_id=document_id,
            source_id=crawl_source_id or document.primary_source_id,
            base_url=document.canonical_url,
            message=source_error,
        )

    if document.discovery_status == "rejected":
        return DiscoveryReport(
            parent_document_id=document_id,
            source_id=source.id,
            base_url=document.canonical_url,
            message=(
                "document discovery_status=rejected; "
                "excluded from discovery and downstream content processing"
            ),
        )

    current = get_current_version(session, document.id)
    if current is None:
        return DiscoveryReport(
            parent_document_id=document_id,
            source_id=source.id,
            base_url=document.canonical_url,
            message="document has no current version to scan",
        )

    try:
        raw = _safe_read_stored_html(root, current.storage_path)
    except (OSError, ValueError) as exc:
        return DiscoveryReport(
            parent_document_id=document_id,
            source_id=source.id,
            base_url=document.canonical_url,
            message=f"cannot read stored HTML: {exc}",
        )

    html = raw.decode("utf-8", errors="replace")
    patterns = list(source.allowed_url_patterns or [])
    links = discover_links_from_html(
        html,
        base_url=document.canonical_url,
        allowed_patterns=patterns,
    )
    # Stable ordering; cap listing size.
    links = sorted(
        links,
        key=lambda item: (item.kind, item.url_normalized or item.raw_href),
    )[: policy.max_discovered_links_listed]

    report = DiscoveryReport(
        parent_document_id=document_id,
        source_id=source.id,
        base_url=document.canonical_url,
        links=links,
        message=(
            "dry-run: listed in-scope links from stored HTML; "
            "pass --persist to fetch in-scope HTML only"
            if not persist
            else "persist: fetching in-scope HTML links (PDFs skipped in this phase)"
        ),
    )

    if not persist:
        # Represent dry-run candidates as fetch-result rows for CLI clarity.
        for link in report.in_scope_html[
            : policy.max_discovered_html_fetches_per_run
        ]:
            report.fetch_results.append(
                SeedFetchResult(
                    source_id=source.id,
                    source_name=source.name,
                    seed_url=link.url_normalized or "",
                    status=STATUS_DRY_RUN,
                    message="dry-run: would fetch discovered HTML URL",
                )
            )
        return report

    host_times: dict[str, float] = {}
    html_links = report.in_scope_html[: policy.max_discovered_html_fetches_per_run]
    for link in html_links:
        if not link.url_normalized:
            continue
        # Skip the parent page itself.
        if link.url_normalized == document.url_normalized:
            report.fetch_results.append(
                SeedFetchResult(
                    source_id=source.id,
                    source_name=source.name,
                    seed_url=link.url_normalized,
                    status=STATUS_SKIPPED,
                    document_id=document.id,
                    message="skipped parent document URL",
                )
            )
            continue
        result = fetch_html_document_url(
            session,
            source,
            url=link.url_normalized,
            discovery_status="discovered",
            persist=True,
            policy=policy,
            http_client=http_client,
            storage_root=root,
            apply_rate_limit=True,
            last_host_fetch_at=host_times,
        )
        report.fetch_results.append(result)

    return report


def discover_from_source_seed(
    session: Session,
    source_id: int,
    *,
    persist: bool = False,
    policy: CrawlPolicy | None = None,
    http_client=None,
    storage_root: Path | str | None = None,
) -> DiscoveryReport:
    """Discover links from the stored seed document of an approved source.

    The selected ``source_id`` always governs allowlist filtering and fetches,
    even if the seed URL document is primarily owned by another source.
    """
    source = session.get(Source, source_id)
    if source is None:
        return DiscoveryReport(
            parent_document_id=0,
            source_id=source_id,
            base_url="",
            message="source not found",
        )
    if source.review_status != "approved":
        return DiscoveryReport(
            parent_document_id=0,
            source_id=source_id,
            base_url=source.seed_url,
            message=(
                f"source review_status={source.review_status!r}; not approved"
            ),
        )
    document = _document_for_source_seed(session, source)
    if document is None:
        return DiscoveryReport(
            parent_document_id=0,
            source_id=source_id,
            base_url=source.seed_url,
            message=(
                "no stored seed document for this source; "
                "run fetch_seeds.py --persist first"
            ),
        )
    return discover_from_document(
        session,
        document.id,
        crawl_source_id=source.id,
        persist=persist,
        policy=policy,
        http_client=http_client,
        storage_root=storage_root,
    )
