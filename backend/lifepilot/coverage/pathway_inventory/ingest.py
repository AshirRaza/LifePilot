"""Bounded approved-source ingestion for Phase 6G."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from lifepilot.crawl.policy import fetcher_policy
from lifepilot.db.session import get_session_factory
from lifepilot.ingest.pipeline import IngestLimits, run_ingestion

# Prefer weaker-coverage countries; avoid spending all budget on GB/NL/SG.
PRIORITY_SOURCE_PLAN: list[dict[str, Any]] = [
    # US hub — expand under approved working-in-US allowlist
    {"source_id": 9080, "country": "US", "max_fetches": 15, "max_html": 8, "max_binary": 2, "max_depth": 2},
    # DE BAMF / AA (skip blocked Make-it-in-Germany 87/92)
    {"source_id": 11033, "country": "DE", "max_fetches": 8, "max_html": 5, "max_binary": 1, "max_depth": 2},
    {"source_id": 11034, "country": "DE", "max_fetches": 8, "max_html": 5, "max_binary": 1, "max_depth": 2},
    {"source_id": 11035, "country": "DE", "max_fetches": 6, "max_html": 4, "max_binary": 1, "max_depth": 2},
    {"source_id": 11036, "country": "DE", "max_fetches": 6, "max_html": 4, "max_binary": 1, "max_depth": 2},
    # CH
    {"source_id": 9091, "country": "CH", "max_fetches": 12, "max_html": 6, "max_binary": 2, "max_depth": 2},
    # AU thin seeds
    {"source_id": 11024, "country": "AU", "max_fetches": 6, "max_html": 4, "max_binary": 1, "max_depth": 2},
    {"source_id": 11025, "country": "AU", "max_fetches": 6, "max_html": 4, "max_binary": 1, "max_depth": 2},
    {"source_id": 11027, "country": "AU", "max_fetches": 6, "max_html": 4, "max_binary": 1, "max_depth": 2},
    {"source_id": 9082, "country": "AU", "max_fetches": 4, "max_html": 3, "max_binary": 1, "max_depth": 2},
    # CA already rich — small top-up only
    {"source_id": 9083, "country": "CA", "max_fetches": 8, "max_html": 4, "max_binary": 1, "max_depth": 1},
    # NZ approved sources but avoid large frontier backlog processing
    {"source_id": 11028, "country": "NZ", "max_fetches": 6, "max_html": 3, "max_binary": 1, "max_depth": 1},
    {"source_id": 11029, "country": "NZ", "max_fetches": 6, "max_html": 3, "max_binary": 1, "max_depth": 1},
    {"source_id": 11030, "country": "NZ", "max_fetches": 4, "max_html": 3, "max_binary": 1, "max_depth": 1},
    # IE
    {"source_id": 9089, "country": "IE", "max_fetches": 6, "max_html": 4, "max_binary": 1, "max_depth": 2},
    {"source_id": 9090, "country": "IE", "max_fetches": 6, "max_html": 3, "max_binary": 1, "max_depth": 1},
]

GLOBAL_MAX_FETCHES = 100
GLOBAL_MAX_NETWORK = 200
PER_COUNTRY_MAX = 20


def _counts() -> dict[str, int]:
    session = get_session_factory()()
    try:
        return dict(
            session.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM sources WHERE review_status='approved')
                        AS approved_sources,
                      (SELECT count(*) FROM documents) AS documents,
                      (SELECT count(*) FROM document_versions) AS document_versions,
                      (SELECT count(*) FROM document_extractions WHERE status='success')
                        AS success_extractions
                    """
                )
            ).mappings().one()
        )
    finally:
        session.close()


def run_bounded_ingestion(*, execute: bool = True) -> dict[str, Any]:
    before = _counts()
    remaining_fetches = GLOBAL_MAX_FETCHES
    remaining_network = GLOBAL_MAX_NETWORK
    per_country = {c: PER_COUNTRY_MAX for c in {p["country"] for p in PRIORITY_SOURCE_PLAN}}

    runs: list[dict[str, Any]] = []
    total_attempted = 0
    total_new = 0
    total_failed = 0
    total_blocked = 0
    attempted_urls: list[dict[str, Any]] = []

    factory = get_session_factory()
    for plan in PRIORITY_SOURCE_PLAN:
        country = plan["country"]
        if remaining_fetches <= 0 or remaining_network <= 0:
            runs.append({"source_id": plan["source_id"], "skipped": "global_budget_exhausted"})
            break
        country_left = per_country.get(country, 0)
        if country_left <= 0:
            runs.append({"source_id": plan["source_id"], "skipped": "per_country_budget_exhausted"})
            continue

        budget = min(
            plan["max_fetches"],
            remaining_fetches,
            country_left,
            remaining_network,
        )
        if budget <= 0:
            continue

        limits = IngestLimits(
            max_sources=1,
            max_fetches=budget,
            max_html_per_source=min(plan["max_html"], budget),
            max_binary_per_source=min(plan["max_binary"], budget),
            max_extractions=max(budget * 2, 10),
            max_comparisons=5,
            max_depth=plan["max_depth"],
        )
        policy = fetcher_policy(
            max_discovered_html_fetches_per_run=limits.max_html_per_source,
            max_discovered_binary_fetches_per_run=limits.max_binary_per_source,
            max_depth=limits.max_depth,
        )

        with factory() as session:
            report = run_ingestion(
                session,
                execute=execute,
                mode="full",
                source_ids=[plan["source_id"]],
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
            data = report.to_dict() if hasattr(report, "to_dict") else {}

        attempted = int(getattr(report, "fetches_attempted", 0) or data.get("fetches_attempted") or 0)
        new_v = int(getattr(report, "fetches_new_version", 0) or 0)
        failed = int(getattr(report, "fetches_failed", 0) or 0)
        blocked = int(getattr(report, "fetches_blocked", 0) or 0)
        urls = list(getattr(report, "attempted_urls", None) or data.get("attempted_urls") or [])

        total_attempted += attempted
        total_new += new_v
        total_failed += failed
        total_blocked += blocked
        attempted_urls.extend(urls[:50])

        # Network attempts ≈ fetch attempts + robots checks (approx +1 per source)
        used_network = attempted + 1
        remaining_fetches = max(0, remaining_fetches - attempted)
        remaining_network = max(0, remaining_network - used_network)
        per_country[country] = max(0, country_left - attempted)

        runs.append(
            {
                "source_id": plan["source_id"],
                "country": country,
                "budget": budget,
                "fetches_attempted": attempted,
                "fetches_new_version": new_v,
                "fetches_failed": failed,
                "fetches_blocked": blocked,
                "notes": list(getattr(report, "notes", []) or [])[:5],
                "exclusions": list(getattr(report, "exclusions", []) or [])[:5],
            }
        )

    after = _counts()
    return {
        "phase": "6G",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execute": execute,
        "limits": {
            "max_fetch_attempts": GLOBAL_MAX_FETCHES,
            "max_network_attempts": GLOBAL_MAX_NETWORK,
            "max_discovery_depth": 2,
            "max_per_country": PER_COUNTRY_MAX,
            "unapproved_source_expansion": False,
            "held_out_of_allowlist_urls": [
                "https://enterprise.gov.ie/en/publications/publication-files/critical-skills-employment-permits-checklist.pdf",
                "https://enterprise.gov.ie/en/Publications/Publication-files/General-Employment-Permits-Checklist.pdf",
                "https://ind.nl/en/forms/6009.pdf",
            ],
        },
        "before_counts": before,
        "after_counts": after,
        "delta": {
            k: int(after[k]) - int(before[k])
            for k in (
                "approved_sources",
                "documents",
                "document_versions",
                "success_extractions",
            )
        },
        "totals": {
            "fetches_attempted": total_attempted,
            "fetches_new_version": total_new,
            "fetches_failed": total_failed,
            "fetches_blocked": total_blocked,
            "remaining_fetch_budget": remaining_fetches,
            "remaining_network_budget": remaining_network,
        },
        "per_source_runs": runs,
        "attempted_urls_sample": attempted_urls[:80],
        "summary": (
            f"Bounded ingestion attempted={total_attempted} new_versions={total_new} "
            f"failed={total_failed} blocked={total_blocked}."
        ),
    }


def dumps_ingestion(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n"
