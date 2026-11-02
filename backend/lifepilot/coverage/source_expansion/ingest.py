"""Bounded Phase 6H ingestion for owner-approved sources only."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lifepilot.crawl.policy import fetcher_policy
from lifepilot.db.models import DiscoveryFrontier, Source
from lifepilot.db.session import get_session_factory
from lifepilot.ingest.pipeline import IngestLimits, run_ingestion
from lifepilot.registry.urls import normalize_url

GLOBAL_MAX_FETCHES = 100
GLOBAL_MAX_NETWORK = 200
PER_COUNTRY_MAX = 20
MAX_DEPTH = 2

# Ordered by evidence value. Keyed by candidate_id (sources resolved after approval).
INGEST_PLAN: list[dict[str, Any]] = [
    # IE exact checklist PDFs + checklist index (3 exact URLs; small buffer for linked scope).
    {"key": "IE-9090", "country": "IE", "candidate_ids": ["IE-critical-skills-checklist"], "source_id": 9090, "max_fetches": 5, "max_html": 2, "max_binary": 3},
    # NL form 6009.pdf
    {"key": "NL-11039", "country": "NL", "candidate_ids": ["NL-ind-form-6009"], "source_id": 11039, "max_fetches": 2, "max_html": 1, "max_binary": 1},
    # New narrow sources (source ids resolved from approval actions)
    {"key": "GB-student", "country": "GB", "candidate_ids": ["GB-student-visa"], "max_fetches": 10, "max_html": 10, "max_binary": 1},
    {"key": "DE-bluecard", "country": "DE", "candidate_ids": ["DE-bamf-eu-blue-card"], "max_fetches": 3, "max_html": 3, "max_binary": 1},
    {"key": "AU-485", "country": "AU", "candidate_ids": ["AU-temporary-graduate-485"], "max_fetches": 6, "max_html": 6, "max_binary": 1},
    {"key": "AE-portal", "country": "AE", "candidate_ids": ["AE-uae-portal-visa-emirates-id"], "max_fetches": 8, "max_html": 8, "max_binary": 1},
]

TARGETED_EXACT: list[dict[str, Any]] = [
    {
        "key": "US-students-and-employment",
        "country": "US",
        "candidate_id": "US-uscis-students-and-employment",
        "source_id": 9080,
        "url": (
            "https://www.uscis.gov/working-in-the-united-states/"
            "students-and-exchange-visitors/students-and-employment"
        ),
    }
]


def counts(session: Session) -> dict[str, int]:
    row = session.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM sources WHERE review_status='approved')
                AS approved_sources,
              (SELECT count(*) FROM documents) AS documents,
              (SELECT count(*) FROM document_versions) AS document_versions,
              (SELECT count(*) FROM document_extractions WHERE status='success')
                AS success_extractions,
              (SELECT coalesce(max(id),0) FROM documents) AS max_document_id,
              (SELECT coalesce(max(id),0) FROM document_versions) AS max_version_id
            """
        )
    ).mappings().one()
    return {k: int(v) for k, v in row.items()}


def reactivate_exact_frontier_rows(session: Session, source: Source) -> list[int]:
    """Re-open out_of_scope rows for exact URLs now explicitly allowlisted."""
    from lifepilot.crawl.allowlist_exact import iter_exact_allowlist_urls

    reopened: list[int] = []
    for item in iter_exact_allowlist_urls(source.allowed_url_patterns):
        row = session.scalar(
            select(DiscoveryFrontier).where(
                DiscoveryFrontier.source_id == source.id,
                DiscoveryFrontier.url_normalized == item.url_normalized,
            )
        )
        if row is not None and row.status == "out_of_scope":
            row.status = "pending"
            row.attempt_count = 0
            row.last_error = None
            row.failure_class = None
            row.next_attempt_at = None
            reopened.append(row.id)
    session.flush()
    return reopened


def targeted_exact_fetch(
    session: Session,
    source: Source,
    *,
    url: str,
    policy,
    execute: bool = True,
    http_client=None,
) -> dict[str, Any]:
    """Fetch one owner-authorized URL already within the source allowlist.

    Uses the standard fetcher (approval, allowlist, robots, redirect checks). Does not
    expand children (depth is capped and enqueue budget is zero).
    """
    from lifepilot.crawl.fetcher import STATUS_FETCHED_NEW_VERSION, commit_fetch_result
    from lifepilot.crawl.frontier import enqueue_url, get_frontier_row
    from lifepilot.crawl.frontier_crawl import (
        FrontierCrawlReport,
        _apply_fetch_to_frontier,
        _fetch_frontier_row,
    )
    from lifepilot.crawl.storage import data_root

    normalized = normalize_url(url)
    out: dict[str, Any] = {
        "url": normalized,
        "source_id": source.id,
        "executed": execute,
        "status": "not_attempted",
        "fetches_attempted": 0,
    }
    row = get_frontier_row(session, source.id, normalized)
    if row is None:
        enq = enqueue_url(
            session,
            source,
            url=normalized,
            discovery_method="html_link",
            depth=policy.max_depth,
            parent_url=source.seed_url_normalized,
            content_kind_hint="html_page",
        )
        row = enq.row
        out["frontier_enqueue"] = enq.outcome
    if row is None:
        out["status"] = "enqueue_rejected"
        return out
    out["frontier_row_id"] = row.id
    out["frontier_status_before"] = row.status
    if row.status != "pending":
        out["status"] = f"skipped_frontier_status_{row.status}"
        return out
    if not execute:
        out["status"] = "dry_run"
        return out
    report = FrontierCrawlReport(source_id=source.id)
    root = data_root(None)
    result = _fetch_frontier_row(
        session,
        source,
        row,
        policy=policy,
        http_client=http_client,
        storage_root=root,
        host_times={},
    )
    result = commit_fetch_result(session, result, storage_root=root)
    _apply_fetch_to_frontier(
        session,
        source,
        row,
        result,
        policy=policy,
        enqueue_budget=[0],
        report=report,
        storage_root=root,
    )
    session.flush()
    out.update(
        {
            "status": result.status,
            "message": result.message,
            "http_status": result.http_status,
            "document_id": result.document_id,
            "fetches_attempted": 1,
            "new_version": result.status == STATUS_FETCHED_NEW_VERSION,
            "children_enqueued": report.children_enqueued,
            "depth_capped_expansions": report.depth_capped_expansions,
        }
    )
    return out


def collect_new_versions(
    session: Session, *, before_max_version_id: int
) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT d.id AS document_id, d.canonical_url, d.primary_source_id AS source_id,
                   d.content_kind, d.language AS document_language,
                   v.id AS version_id, v.version_number, v.source_url, v.content_type,
                   v.http_status, v.language, v.content_sha256, v.byte_size,
                   v.storage_path, v.retrieved_at, v.is_current,
                   COALESCE(j.country_code, j.code) AS country,
                   (SELECT e.status FROM document_extractions e
                     WHERE e.document_version_id = v.id
                     ORDER BY e.id DESC LIMIT 1) AS extraction_status,
                   (SELECT e.message FROM document_extractions e
                     WHERE e.document_version_id = v.id
                     ORDER BY e.id DESC LIMIT 1) AS extraction_message
            FROM document_versions v
            JOIN documents d ON d.id = v.document_id
            LEFT JOIN sources s ON s.id = d.primary_source_id
            LEFT JOIN jurisdictions j ON j.id = s.jurisdiction_id
            WHERE v.id > :vid
            ORDER BY v.id
            """
        ),
        {"vid": before_max_version_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def run_bounded_ingestion(
    *,
    source_by_candidate: dict[str, int],
    approved_candidate_ids: set[str],
    execute: bool = True,
) -> dict[str, Any]:
    """Run the bounded 6H plan. ``source_by_candidate`` comes from approval actions."""
    factory = get_session_factory()
    started = datetime.now(timezone.utc).isoformat()
    with factory() as session:
        before = counts(session)

    remaining_fetches = GLOBAL_MAX_FETCHES
    remaining_network = GLOBAL_MAX_NETWORK
    per_country: dict[str, int] = {}
    country_attempts: dict[str, int] = {}
    runs: list[dict[str, Any]] = []
    attempted_urls: list[dict[str, Any]] = []
    skipped_reasons: list[dict[str, Any]] = []
    totals = {"fetches_attempted": 0, "fetches_new_version": 0, "fetches_failed": 0,
              "fetches_blocked": 0, "sitemap_fetches": 0}

    def country_left(country: str) -> int:
        return PER_COUNTRY_MAX - country_attempts.get(country, 0)

    for plan in INGEST_PLAN:
        cids = plan["candidate_ids"]
        if not all(c in approved_candidate_ids for c in cids):
            skipped_reasons.append(
                {"key": plan["key"], "reason": "candidate_not_approved_no_fetch"}
            )
            continue
        source_id = plan.get("source_id") or source_by_candidate.get(cids[0])
        if source_id is None:
            skipped_reasons.append({"key": plan["key"], "reason": "source_not_registered"})
            continue
        country = plan["country"]
        budget = min(plan["max_fetches"], remaining_fetches, country_left(country), remaining_network)
        if budget <= 0:
            skipped_reasons.append({"key": plan["key"], "reason": "budget_exhausted"})
            continue
        limits = IngestLimits(
            max_sources=1,
            max_fetches=budget,
            max_html_per_source=min(plan["max_html"], budget),
            max_binary_per_source=min(plan["max_binary"], budget),
            max_extractions=max(budget * 2, 10),
            max_comparisons=5,
            max_depth=MAX_DEPTH,
        )
        policy = fetcher_policy(
            max_discovered_html_fetches_per_run=limits.max_html_per_source,
            max_discovered_binary_fetches_per_run=limits.max_binary_per_source,
            max_depth=limits.max_depth,
        )
        with factory() as session:
            source = session.get(Source, source_id)
            reopened = reactivate_exact_frontier_rows(session, source) if source else []
            report = run_ingestion(
                session,
                execute=execute,
                mode="full",
                source_ids=[source_id],
                all_approved=False,
                country=None,
                limits=limits,
                policy=policy,
                storage_root=None,
                write_report_file=False,
            )
            if execute:
                session.commit()
            else:
                session.rollback()
        attempted = int(report.fetches_attempted or 0)
        sitemap = int(getattr(report, "sitemap_fetches_attempted", 0) or 0)
        used_network = attempted + sitemap + 1  # +1 robots preflight (approx.)
        remaining_fetches = max(0, remaining_fetches - attempted)
        remaining_network = max(0, remaining_network - used_network)
        country_attempts[country] = country_attempts.get(country, 0) + attempted
        totals["fetches_attempted"] += attempted
        totals["fetches_new_version"] += int(report.fetches_new_version or 0)
        totals["fetches_failed"] += int(report.fetches_failed or 0)
        totals["fetches_blocked"] += int(report.fetches_blocked or 0)
        totals["sitemap_fetches"] += sitemap
        attempted_urls.extend(list(report.attempted_urls or [])[:60])
        runs.append(
            {
                "key": plan["key"],
                "source_id": source_id,
                "country": country,
                "budget": budget,
                "fetches_attempted": attempted,
                "fetches_new_version": int(report.fetches_new_version or 0),
                "fetches_unchanged": int(report.fetches_unchanged or 0),
                "fetches_failed": int(report.fetches_failed or 0),
                "fetches_blocked": int(report.fetches_blocked or 0),
                "extractions_success": int(report.extractions_success or 0),
                "extractions_failed": int(report.extractions_failed or 0),
                "extractions_empty": int(report.extractions_empty or 0),
                "sitemap_fetches": sitemap,
                "frontier_pending_remaining": report.frontier_pending_remaining,
                "frontier_out_of_scope": report.frontier_out_of_scope,
                "out_of_scope_links_rejected": report.out_of_scope_links_rejected,
                "reopened_frontier_rows": reopened,
                "limits_reached": list(report.limits_reached or []),
                "notes": list(report.notes or [])[:6],
                "exclusions": list(report.exclusions or [])[:6],
                "failures": [
                    f.to_dict() if hasattr(f, "to_dict") else f
                    for f in list(report.failures or [])[:6]
                ],
            }
        )

    targeted: list[dict[str, Any]] = []
    for t in TARGETED_EXACT:
        if t["candidate_id"] not in approved_candidate_ids:
            skipped_reasons.append({"key": t["key"], "reason": "candidate_not_approved_no_fetch"})
            continue
        if remaining_fetches <= 0 or country_left(t["country"]) <= 0 or remaining_network <= 0:
            skipped_reasons.append({"key": t["key"], "reason": "budget_exhausted"})
            continue
        policy = fetcher_policy(
            max_discovered_html_fetches_per_run=1,
            max_discovered_binary_fetches_per_run=0,
            max_depth=MAX_DEPTH,
        )
        with factory() as session:
            source = session.get(Source, t["source_id"])
            res = targeted_exact_fetch(session, source, url=t["url"], policy=policy, execute=execute)
            if execute:
                session.commit()
            else:
                session.rollback()
        att = int(res.get("fetches_attempted", 0))
        remaining_fetches -= att
        remaining_network -= att + 1
        country_attempts[t["country"]] = country_attempts.get(t["country"], 0) + att
        totals["fetches_attempted"] += att
        totals["fetches_new_version"] += 1 if res.get("new_version") else 0
        if att and not res.get("new_version") and res.get("status") not in {"unchanged"}:
            totals["fetches_failed"] += 1
        targeted.append({**t, **res})

    # Extraction pass for fetched documents (no network; bounded) in process-existing mode.
    extraction_runs: list[dict[str, Any]] = []
    touched_sources = sorted({r["source_id"] for r in runs} | {t["source_id"] for t in targeted})
    for sid in touched_sources:
        limits = IngestLimits(
            max_sources=1, max_fetches=0, max_html_per_source=0, max_binary_per_source=0,
            max_extractions=40, max_comparisons=5, max_depth=MAX_DEPTH,
        )
        with factory() as session:
            rep = run_ingestion(
                session, execute=execute, mode="process-existing", source_ids=[sid],
                all_approved=False, country=None, limits=limits,
                policy=fetcher_policy(max_depth=MAX_DEPTH), storage_root=None,
                write_report_file=False,
            )
            if execute:
                session.commit()
            else:
                session.rollback()
        extraction_runs.append(
            {
                "source_id": sid,
                "extractions_success": int(rep.extractions_success or 0),
                "extractions_failed": int(rep.extractions_failed or 0),
                "extractions_empty": int(rep.extractions_empty or 0),
                "extractions_skipped": int(rep.extractions_skipped or 0),
                "comparisons_changed": int(rep.comparisons_changed or 0),
                "comparisons_unchanged": int(rep.comparisons_unchanged or 0),
            }
        )

    with factory() as session:
        after = counts(session)
        new_versions = collect_new_versions(session, before_max_version_id=before["max_version_id"])

    unused = {
        "fetch_attempts_unused": remaining_fetches,
        "network_attempts_unused": remaining_network,
        "reason": (
            "Bounded to owner-authorized sources/URLs only: IE/NL exact URLs, new GB/DE/AU/AE "
            "narrow sources and one US exact URL. JP/FR/NZ candidates were rejected and no "
            "substitute URLs were fetched; no automatic expansion beyond approved scopes; "
            "per-source caps limited discovery depth 2."
        ),
    }
    delta_keys = ("approved_sources", "documents", "document_versions", "success_extractions")
    return {
        "phase": "6H",
        "started_at": started,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execute": execute,
        "limits": {
            "max_fetch_attempts": GLOBAL_MAX_FETCHES,
            "max_network_attempts": GLOBAL_MAX_NETWORK,
            "max_per_country": PER_COUNTRY_MAX,
            "max_discovery_depth": MAX_DEPTH,
            "wildcard_or_whole_domain_approval": False,
            "unapproved_url_fetches": False,
        },
        "before_counts": before,
        "after_counts": after,
        "delta": {k: after[k] - before[k] for k in delta_keys},
        "totals": {
            **totals,
            "remaining_fetch_budget": remaining_fetches,
            "remaining_network_budget": remaining_network,
            "attempts_by_country": country_attempts,
            "network_attempts_note": (
                "network attempts = document fetches + sitemap HTTP + 1 robots preflight per "
                "source run (approximate)"
            ),
        },
        "unused_budget": unused,
        "per_source_runs": runs,
        "targeted_exact_fetches": targeted,
        "extraction_runs": extraction_runs,
        "skipped": skipped_reasons,
        "attempted_urls": attempted_urls,
        "new_document_versions": new_versions,
        "summary": (
            f"6H ingestion attempted={totals['fetches_attempted']} "
            f"new_versions={totals['fetches_new_version']} failed={totals['fetches_failed']} "
            f"blocked={totals['fetches_blocked']}."
        ),
    }
