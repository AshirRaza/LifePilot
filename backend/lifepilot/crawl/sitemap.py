"""Bounded official sitemap discovery for approved sources.

Sitemap membership never widens allowlists. Every ``<loc>`` is still subject
to source allowlist, trap filters, robots, and normal fetch validation.
"""

from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

import certifi
import httpx
from sqlalchemy.orm import Session

from lifepilot.crawl.frontier import EnqueueResult, enqueue_url
from lifepilot.crawl.net_safety import url_has_blocked_fetch_host
from lifepilot.crawl.policy import CrawlPolicy, fetcher_policy
from lifepilot.crawl.robots import check_robots
from lifepilot.db.models import Source
from lifepilot.registry.urls import (
    InvalidRegistryUrlError,
    normalize_url,
    url_in_any_allowed_pattern,
)


@dataclass
class SitemapDiscoveryReport:
    source_id: int
    sitemap_fetches_attempted: int = 0
    sitemap_fetches_ok: int = 0
    sitemap_fetches_failed: int = 0
    urls_enqueued_pending: int = 0
    urls_out_of_scope: int = 0
    urls_skipped: int = 0
    urls_unchanged: int = 0
    sitemap_urls_considered: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stopped_due_to_limits: bool = False


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _text(el: Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _child_locs(root: Element) -> tuple[str, list[str]]:
    """Return (kind, locs) where kind is ``urlset`` | ``sitemapindex`` | ``unknown``."""
    kind = _local(root.tag).lower()
    locs: list[str] = []
    if kind == "urlset":
        for child in root:
            if _local(child.tag).lower() != "url":
                continue
            for sub in child:
                if _local(sub.tag).lower() == "loc":
                    text = _text(sub)
                    if text:
                        locs.append(text)
                    break
        return "urlset", locs
    if kind == "sitemapindex":
        for child in root:
            if _local(child.tag).lower() != "sitemap":
                continue
            for sub in child:
                if _local(sub.tag).lower() == "loc":
                    text = _text(sub)
                    if text:
                        locs.append(text)
                    break
        return "sitemapindex", locs
    # Best-effort: any <loc> elements.
    for el in root.iter():
        if _local(el.tag).lower() == "loc":
            text = _text(el)
            if text:
                locs.append(text)
    return "unknown", locs


def parse_sitemap_xml(body: bytes) -> tuple[str, list[str]]:
    """Parse sitemap or sitemap-index XML bytes → (kind, loc URLs)."""
    # Strip BOM / leading whitespace noise.
    text_bytes = body.lstrip()
    if text_bytes.startswith(b"\x1f\x8b"):
        text_bytes = gzip.decompress(text_bytes)
    root = ET.fromstring(text_bytes)
    return _child_locs(root)


def _seed_host(source: Source) -> str:
    return (urlsplit(source.seed_url_normalized or source.seed_url).hostname or "").lower()


def _candidate_sitemap_urls(source: Source, policy: CrawlPolicy, client) -> list[str]:
    seed = source.seed_url_normalized or source.seed_url
    robots = check_robots(seed, policy, client=client)
    candidates: list[str] = []
    seen: set[str] = set()
    for url in robots.sitemap_urls:
        if url not in seen:
            seen.add(url)
            candidates.append(url)
    # Conventional location on the seed host only.
    host = _seed_host(source)
    if host:
        parts = urlsplit(seed)
        conventional = f"{parts.scheme}://{host}/sitemap.xml"
        if conventional not in seen:
            candidates.append(conventional)
    return candidates


def _fetch_sitemap_body(
    url: str,
    *,
    source: Source,
    policy: CrawlPolicy,
    client: httpx.Client,
) -> tuple[bytes | None, str]:
    patterns = list(source.allowed_url_patterns or [])
    # Sitemap files themselves may live outside document path allowlists
    # (e.g. /sitemap.xml) but must stay on the seed host.
    try:
        normalized = normalize_url(url)
    except InvalidRegistryUrlError as exc:
        return None, str(exc)
    if url_has_blocked_fetch_host(normalized):
        return None, "blocked host"
    host = (urlsplit(normalized).hostname or "").lower()
    if host != _seed_host(source):
        # Allow sitemap on seed host even if path not in document allowlist.
        return None, "sitemap host mismatch"
    # Prefer robots allow for the sitemap URL.
    robots = check_robots(normalized, policy, client=client)
    if policy.require_robots_ok and not robots.fetched:
        return None, f"robots unavailable: {robots.error}"
    if robots.can_fetch is False:
        return None, "robots disallows sitemap URL"

    # Document allowlist is NOT required for fetching the sitemap file itself
    # (only for enqueued <loc> targets). Still require same host.
    try:
        response = client.get(
            normalized,
            headers={
                "User-Agent": policy.user_agent,
                "Accept": "application/xml,text/xml,application/gzip,*/*;q=0.1",
            },
        )
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"

    if response.status_code >= 400:
        return None, f"HTTP {response.status_code}"
    body = response.content
    if len(body) > policy.max_sitemap_response_bytes:
        return None, "sitemap exceeds max_sitemap_response_bytes"
    # httpx may already decompress Content-Encoding: gzip. Only gunzip when
    # the payload still looks compressed (magic bytes), including .xml.gz URLs.
    if body.startswith(b"\x1f\x8b"):
        try:
            body = gzip.decompress(body)
        except OSError as exc:
            return None, f"gzip decompress failed: {exc}"
    return body, ""


def discover_sitemaps_for_source(
    session: Session,
    source: Source,
    *,
    policy: CrawlPolicy | None = None,
    http_client: httpx.Client | None = None,
    network_budget: list[int] | None = None,
    enqueue_budget: list[int] | None = None,
) -> SitemapDiscoveryReport:
    """Fetch bounded sitemaps and enqueue in-scope ``<loc>`` URLs.

    ``network_budget`` is a shared mutable ``[remaining]`` for all HTTP
    (including document fetches). Sitemap attempts decrement it. Document
    HTML/PDF/DOCX caps are separate and not used here.
    """
    policy = policy or fetcher_policy()
    report = SitemapDiscoveryReport(source_id=source.id)
    if source.review_status != "approved":
        report.notes.append(f"source not approved ({source.review_status})")
        return report

    owns_client = http_client is None
    client = http_client or httpx.Client(
        timeout=policy.request_timeout_seconds,
        follow_redirects=True,
        verify=certifi.where(),
    )
    try:
        candidates = _candidate_sitemap_urls(source, policy, client)
        report.sitemap_urls_considered = list(candidates)
        queue: list[tuple[str, int]] = [(u, 0) for u in candidates]
        fetched_docs = 0
        seen_sitemaps: set[str] = set()

        while queue:
            if fetched_docs >= policy.max_sitemap_fetches_per_source:
                report.stopped_due_to_limits = True
                report.notes.append("max_sitemap_fetches_per_source reached")
                break
            if network_budget is not None and network_budget[0] <= 0:
                report.stopped_due_to_limits = True
                report.notes.append("global network budget exhausted")
                break
            if enqueue_budget is not None and enqueue_budget[0] <= 0:
                report.stopped_due_to_limits = True
                report.notes.append("frontier enqueue budget exhausted")
                break
            if (
                report.urls_enqueued_pending
                >= policy.max_sitemap_urls_enqueued_per_run
            ):
                report.stopped_due_to_limits = True
                report.notes.append("max_sitemap_urls_enqueued_per_run reached")
                break

            sm_url, index_depth = queue.pop(0)
            try:
                key = normalize_url(sm_url)
            except InvalidRegistryUrlError:
                key = sm_url
            if key in seen_sitemaps:
                continue
            seen_sitemaps.add(key)

            body, err = _fetch_sitemap_body(
                sm_url, source=source, policy=policy, client=client
            )
            report.sitemap_fetches_attempted += 1
            if network_budget is not None:
                network_budget[0] = max(0, network_budget[0] - 1)
            fetched_docs += 1
            if body is None:
                report.sitemap_fetches_failed += 1
                report.notes.append(f"sitemap fetch failed {sm_url}: {err}")
                continue
            report.sitemap_fetches_ok += 1

            try:
                kind, locs = parse_sitemap_xml(body)
            except ET.ParseError as exc:
                report.notes.append(f"sitemap XML parse error {sm_url}: {exc}")
                continue

            if kind == "sitemapindex":
                if index_depth >= policy.max_sitemap_index_depth:
                    report.notes.append(
                        f"sitemap index depth cap at {sm_url}"
                    )
                    continue
                for loc in locs:
                    queue.append((loc, index_depth + 1))
                continue

            patterns = list(source.allowed_url_patterns or [])
            for loc in locs:
                if (
                    report.urls_enqueued_pending
                    >= policy.max_sitemap_urls_enqueued_per_run
                ):
                    report.stopped_due_to_limits = True
                    break
                if enqueue_budget is not None and enqueue_budget[0] <= 0:
                    report.stopped_due_to_limits = True
                    break
                try:
                    loc_norm = normalize_url(loc)
                except InvalidRegistryUrlError:
                    continue
                # Only persist in-scope (or trap-skipped) URLs. Recording every
                # out-of-scope sitemap loc explodes the frontier without benefit.
                if not url_in_any_allowed_pattern(loc_norm, patterns):
                    report.urls_out_of_scope += 1
                    continue
                result = enqueue_url(
                    session,
                    source,
                    url=loc_norm,
                    discovery_method="sitemap",
                    depth=1,
                    parent_url=sm_url,
                )
                _tally_enqueue(report, result, enqueue_budget)
    finally:
        if owns_client:
            client.close()

    return report


def _tally_enqueue(
    report: SitemapDiscoveryReport,
    result: EnqueueResult,
    enqueue_budget: list[int] | None,
) -> None:
    if result.outcome == "inserted_pending":
        report.urls_enqueued_pending += 1
        if enqueue_budget is not None:
            enqueue_budget[0] = max(0, enqueue_budget[0] - 1)
    elif result.outcome == "inserted_out_of_scope":
        report.urls_out_of_scope += 1
    elif result.outcome == "inserted_skipped":
        report.urls_skipped += 1
    elif result.outcome == "unchanged":
        report.urls_unchanged += 1


__all__ = [
    "SitemapDiscoveryReport",
    "discover_sitemaps_for_source",
    "parse_sitemap_xml",
]
