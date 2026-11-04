"""Diagnose and repair empty frontiers without widening authorization."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lifepilot.crawl.frontier_crawl import FrontierCrawlReport, _expand_html_children
from lifepilot.crawl.policy import fetcher_policy
from lifepilot.crawl.storage import data_root
from lifepilot.db.models import DiscoveryFrontier, Document, Source


def diagnose_frontiers(session: Session) -> dict[str, Any]:
    """Summarize pending/fetched/blocked/failed frontier state by country."""
    rows = session.execute(
        text(
            """
            SELECT coalesce(j.code, '??') AS country,
                   s.id AS source_id,
                   s.name AS source_name,
                   s.allowed_url_patterns AS patterns,
                   sum(CASE WHEN f.status = 'pending' THEN 1 ELSE 0 END) AS pending,
                   sum(CASE WHEN f.status = 'fetched' THEN 1 ELSE 0 END) AS fetched,
                   sum(CASE WHEN f.status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   sum(CASE WHEN f.status = 'blocked' THEN 1 ELSE 0 END) AS blocked,
                   sum(CASE WHEN f.status = 'out_of_scope' THEN 1 ELSE 0 END) AS out_of_scope,
                   sum(CASE WHEN f.discovery_method = 'sitemap' THEN 1 ELSE 0 END) AS sitemap_rows,
                   count(f.id) AS frontier_total,
                   max(f.depth) AS max_depth_seen
            FROM sources s
            LEFT JOIN jurisdictions j ON j.id = s.jurisdiction_id
            LEFT JOIN discovery_frontier f ON f.source_id = s.id
            WHERE s.review_status = 'approved'
            GROUP BY 1, 2, 3, 4
            ORDER BY 1, 2
            """
        )
    ).mappings().all()

    by_country: Counter[str] = Counter()
    empty_prefix: list[dict[str, Any]] = []
    empty_exact: list[dict[str, Any]] = []
    blocked_seeds: list[dict[str, Any]] = []
    never_sitemap: list[dict[str, Any]] = []

    for r in rows:
        pending = int(r["pending"] or 0)
        by_country[r["country"]] += pending
        pats = list(r["patterns"] or [])
        is_exact = bool(pats) and all(not str(p).endswith("*") for p in pats)
        entry = {
            "country": r["country"],
            "source_id": int(r["source_id"]),
            "source_name": r["source_name"],
            "pending": pending,
            "fetched": int(r["fetched"] or 0),
            "failed": int(r["failed"] or 0),
            "blocked": int(r["blocked"] or 0),
            "sitemap_rows": int(r["sitemap_rows"] or 0),
            "max_depth_seen": r["max_depth_seen"],
            "exact_allowlist": is_exact,
        }
        if pending == 0:
            if is_exact:
                empty_exact.append(entry)
            else:
                empty_prefix.append(entry)
            if int(r["blocked"] or 0) > 0 and int(r["fetched"] or 0) == 0:
                blocked_seeds.append(entry)
            if int(r["sitemap_rows"] or 0) == 0 and not is_exact:
                never_sitemap.append(entry)

    return {
        "pending_by_country": dict(by_country),
        "empty_prefix_sources": empty_prefix,
        "empty_exact_sources": empty_exact,
        "blocked_seeds_no_docs": blocked_seeds,
        "prefix_never_sitemap": never_sitemap,
        "findings": [
            "Exact-only allowlists cannot discover children (expected authorization limit).",
            "Prefix scopes with pending=0 and never-run sitemaps are a pipeline gap — "
            "sitemap rediscovery should run when pending is empty.",
            "Shallow prior max_depth can leave deeper in-scope links undiscovered; "
            "re-expanding stored HTML at higher max_depth is safe and does not widen allowlists.",
            "WAF/bot-blocked seeds (e.g. make-it-in-germany) must not be repeatedly probed.",
        ],
    }


def reexpand_fetched_html(
    session: Session,
    *,
    source_ids: list[int],
    max_depth: int = 3,
    max_enqueues_per_source: int = 80,
    storage_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Re-expand child links from already-fetched pages without HTTP fetches.

    Fixes empty frontiers caused by prior shallow max_depth without widening
    source authorization. Idempotent: existing frontier URLs are unchanged.
    """
    root = data_root(storage_root)
    policy = fetcher_policy(
        max_depth=max_depth,
        max_frontier_enqueues_per_source=max_enqueues_per_source,
    )
    results: list[dict[str, Any]] = []
    for sid in source_ids:
        source = session.get(Source, sid)
        if source is None or source.review_status != "approved":
            results.append({"source_id": sid, "status": "skipped_not_approved"})
            continue
        pats = list(source.allowed_url_patterns or [])
        if pats and all(not str(p).endswith("*") for p in pats):
            results.append({
                "source_id": sid,
                "status": "skipped_exact_only",
                "note": "Exact allowlist cannot expand children by design",
            })
            continue

        fetched_rows = session.scalars(
            select(DiscoveryFrontier).where(
                DiscoveryFrontier.source_id == sid,
                DiscoveryFrontier.status == "fetched",
                DiscoveryFrontier.depth < max_depth,
            )
        ).all()
        report = FrontierCrawlReport(source_id=sid)
        enqueue_budget = [max_enqueues_per_source]
        before_pending = session.scalar(
            text(
                "SELECT count(*) FROM discovery_frontier "
                "WHERE source_id = :s AND status = 'pending'"
            ),
            {"s": sid},
        )
        expanded_parents = 0
        for frow in fetched_rows:
            if enqueue_budget[0] <= 0:
                break
            doc = session.scalar(
                select(Document).where(
                    Document.url_normalized == frow.url_normalized
                )
            )
            if doc is None:
                continue
            expanded_parents += 1
            _expand_html_children(
                session,
                source,
                document=doc,
                parent_depth=int(frow.depth or 0),
                parent_url=frow.url_normalized,
                policy=policy,
                enqueue_budget=enqueue_budget,
                report=report,
                storage_root=root,
            )
        session.flush()
        after_pending = session.scalar(
            text(
                "SELECT count(*) FROM discovery_frontier "
                "WHERE source_id = :s AND status = 'pending'"
            ),
            {"s": sid},
        )
        results.append({
            "source_id": sid,
            "status": "ok",
            "parents_expanded": expanded_parents,
            "children_enqueued": report.children_enqueued,
            "depth_capped": report.depth_capped_expansions,
            "pending_before": int(before_pending or 0),
            "pending_after": int(after_pending or 0),
        })
    return results


def probe_url_availability(
    url: str,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Lightweight status/redirect probe without storing page content.

    Used for approval-queue hygiene. Does not create documents/versions.
    """
    import httpx

    from lifepilot.registry.urls import normalize_url as norm

    try:
        target = norm(url)
    except Exception as exc:
        return {
            "url": url,
            "ok": False,
            "status": "invalid_url",
            "error": str(exc),
        }

    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": "LifePilotCorpusProbe/6P.2 (+research; no store)"},
        ) as client:
            resp = client.head(target)
            # Some CDNs reject HEAD — fall back to GET without reading body fully.
            if resp.status_code in {405, 403, 501}:
                with client.stream("GET", target) as stream_resp:
                    status = stream_resp.status_code
                    location = stream_resp.headers.get("location")
                    # Drain minimally / close without saving.
                    stream_resp.close()
            else:
                status = resp.status_code
                location = resp.headers.get("location")

        final = target
        redirect_dest = None
        if status in {301, 302, 303, 307, 308} and location:
            redirect_dest = location
            try:
                from urllib.parse import urljoin

                final = norm(urljoin(target, location))
            except Exception:
                final = location

        return {
            "url": target,
            "ok": status is not None and int(status) < 400,
            "http_status": status,
            "redirect_destination": redirect_dest,
            "final_url_normalized": final,
            "same_url_after_redirect": redirect_dest is None or final == target,
            "status": (
                "available"
                if status and int(status) < 400 and redirect_dest is None
                else (
                    "redirect"
                    if redirect_dest
                    else ("unavailable" if status and int(status) >= 400 else "unknown")
                )
            ),
        }
    except Exception as exc:
        return {
            "url": target,
            "ok": False,
            "status": "probe_error",
            "error": str(exc)[:200],
        }
