"""Bounded PDF/DOCX discovery from stored HTML (Phase 4D)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.crawl.discover import _resolve_crawl_source, _safe_read_stored_html
from lifepilot.crawl.fetcher import (
    STATUS_DRY_RUN,
    SeedFetchResult,
    fetch_binary_document_url,
)
from lifepilot.crawl.links import ExtractedLink, discover_links_from_html
from lifepilot.crawl.policy import CrawlPolicy, fetcher_policy
from lifepilot.crawl.storage import data_root
from lifepilot.db.current_version import get_current_version
from lifepilot.db.models import Document, Source


@dataclass
class BinaryDiscoveryReport:
    crawl_source_id: int
    parent_document_ids: list[int] = field(default_factory=list)
    links: list[ExtractedLink] = field(default_factory=list)
    fetch_results: list[SeedFetchResult] = field(default_factory=list)
    message: str = ""

    @property
    def in_scope_pdf(self) -> list[ExtractedLink]:
        return [link for link in self.links if link.kind == "in_scope_pdf"]

    @property
    def in_scope_docx(self) -> list[ExtractedLink]:
        return [link for link in self.links if link.kind == "in_scope_docx"]

    @property
    def out_of_scope_documents(self) -> list[ExtractedLink]:
        """Out-of-scope links whose path looks like pdf/docx (review candidates)."""
        results: list[ExtractedLink] = []
        for link in self.links:
            if link.kind != "out_of_scope" or not link.url_normalized:
                continue
            path = link.url_normalized.lower().split("?", 1)[0]
            if path.endswith(".pdf") or path.endswith(".docx"):
                results.append(link)
        return results


def _document_for_source_seed(session: Session, source: Source) -> Document | None:
    return session.scalar(
        select(Document).where(Document.url_normalized == source.seed_url_normalized)
    )


def _html_parents_for_source(
    session: Session,
    source: Source,
    *,
    include_discovered_html: bool,
) -> list[Document]:
    """HTML documents used as discovery parents for this crawl source.

    Always includes the seed URL document when present. Optionally includes
    other HTML pages whose primary_source_id is this source (for broader
    in-source discovery after HTML crawl). Ownership does not grant allowlist
    rights — filtering still uses ``source.allowed_url_patterns``.
    """
    docs: list[Document] = []
    seen: set[int] = set()
    seed_doc = _document_for_source_seed(session, source)
    if seed_doc is not None and seed_doc.discovery_status != "rejected":
        docs.append(seed_doc)
        seen.add(seed_doc.id)

    if include_discovered_html:
        extra = session.scalars(
            select(Document)
            .where(
                Document.primary_source_id == source.id,
                Document.content_kind == "html_page",
                Document.discovery_status != "rejected",
            )
            .order_by(Document.id)
        ).all()
        for doc in extra:
            if doc.id in seen:
                continue
            docs.append(doc)
            seen.add(doc.id)
    return docs


def discover_binaries_for_source(
    session: Session,
    source_id: int,
    *,
    persist: bool = False,
    policy: CrawlPolicy | None = None,
    http_client=None,
    storage_root: Path | str | None = None,
    include_discovered_html: bool = True,
) -> BinaryDiscoveryReport:
    """Discover PDF/DOCX links from stored HTML under an approved crawl source.

    Dry-run lists candidates. ``persist`` downloads eligible binaries only.
    """
    policy = policy or fetcher_policy()
    root = data_root(storage_root)
    source = session.get(Source, source_id)
    if source is None:
        return BinaryDiscoveryReport(
            crawl_source_id=source_id,
            message="source not found",
        )
    if source.review_status != "approved":
        return BinaryDiscoveryReport(
            crawl_source_id=source_id,
            message=(
                f"source review_status={source.review_status!r}; not approved"
            ),
        )

    parents = _html_parents_for_source(
        session, source, include_discovered_html=include_discovered_html
    )
    if not parents:
        return BinaryDiscoveryReport(
            crawl_source_id=source_id,
            message=(
                "no stored HTML parent documents for this source; "
                "run fetch_seeds / HTML discovery first"
            ),
        )

    patterns = list(source.allowed_url_patterns or [])
    collected: list[ExtractedLink] = []
    seen_urls: set[str] = set()
    parent_ids: list[int] = []

    for parent in parents:
        # Ensure crawl source is the selected source (not inherited owner scope).
        crawl_source, err = _resolve_crawl_source(
            session, parent, crawl_source_id=source.id
        )
        if crawl_source is None:
            continue
        current = get_current_version(session, parent.id)
        if current is None:
            continue
        # Only scan HTML storage paths.
        if not current.storage_path.endswith(".html"):
            continue
        try:
            raw = _safe_read_stored_html(root, current.storage_path)
        except (OSError, ValueError):
            continue
        html = raw.decode("utf-8", errors="replace")
        links = discover_links_from_html(
            html,
            base_url=parent.canonical_url,
            allowed_patterns=patterns,
        )
        parent_ids.append(parent.id)
        for link in links:
            key = link.url_normalized or f"{link.kind}:{link.raw_href}"
            if key in seen_urls:
                continue
            seen_urls.add(key)
            collected.append(link)

    collected = sorted(
        collected,
        key=lambda item: (item.kind, item.url_normalized or item.raw_href),
    )[: policy.max_discovered_links_listed]

    report = BinaryDiscoveryReport(
        crawl_source_id=source.id,
        parent_document_ids=parent_ids,
        links=collected,
        message=(
            "dry-run: listed PDF/DOCX candidates from stored HTML; "
            "pass --persist to download in-scope binaries only"
            if not persist
            else "persist: fetching in-scope PDF/DOCX (HTML not re-fetched)"
        ),
    )

    eligible: list[tuple[ExtractedLink, str]] = []
    for link in report.in_scope_pdf:
        eligible.append((link, "pdf"))
    for link in report.in_scope_docx:
        eligible.append((link, "docx"))
    eligible = eligible[: policy.max_discovered_binary_fetches_per_run]

    if not persist:
        for link, kind in eligible:
            report.fetch_results.append(
                SeedFetchResult(
                    source_id=source.id,
                    source_name=source.name,
                    seed_url=link.url_normalized or "",
                    status=STATUS_DRY_RUN,
                    message=f"dry-run: would fetch discovered {kind}",
                )
            )
        return report

    host_times: dict[str, float] = {}
    for link, kind in eligible:
        if not link.url_normalized:
            continue
        result = fetch_binary_document_url(
            session,
            source,
            url=link.url_normalized,
            expected_kind=kind,
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
