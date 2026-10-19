"""Dry-run crawl planner for approved LifePilot sources.

Checks allowlists + robots.txt and prints what a future fetcher would do.
Does not download government documents or write document rows.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.crawl.planner import build_crawl_plan  # noqa: E402
from lifepilot.crawl.policy import CrawlPolicy  # noqa: E402
from lifepilot.db.session import get_session_factory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run crawl plan for approved sources. "
            "Fetches robots.txt only; does not download pages."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Override default crawl delay seconds (policy default applies otherwise).",
    )
    args = parser.parse_args()

    policy = CrawlPolicy()
    if args.delay is not None:
        policy = CrawlPolicy(default_crawl_delay_seconds=args.delay)

    factory = get_session_factory()
    with factory() as session:
        plan = build_crawl_plan(session, policy=policy)

    if args.json:
        payload = {
            "user_agent": plan.policy.user_agent,
            "planned": plan.planned_count,
            "blocked": plan.blocked_count,
            "entries": [
                {
                    "source_id": e.source_id,
                    "source_name": e.source_name,
                    "seed_url": e.seed_url,
                    "decision": e.decision,
                    "would_fetch": e.would_fetch,
                    "in_allowlist": e.in_allowlist,
                    "crawl_delay_seconds": e.crawl_delay_seconds,
                    "max_requests_per_host_per_minute": (
                        e.max_requests_per_host_per_minute
                    ),
                    "robots": None
                    if e.robots is None
                    else {
                        "robots_url": e.robots.robots_url,
                        "fetched": e.robots.fetched,
                        "status_code": e.robots.status_code,
                        "can_fetch": e.robots.can_fetch,
                        "crawl_delay_seconds": e.robots.crawl_delay_seconds,
                        "error": e.robots.error,
                    },
                    "notes": e.notes,
                }
                for e in plan.entries
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print("LifePilot dry-run crawl plan")
    print(f"user_agent: {plan.policy.user_agent}")
    print(f"approved_sources: {len(plan.entries)}")
    print(f"planned_fetches: {plan.planned_count}")
    print(f"blocked_or_skipped: {plan.blocked_count}")
    print(
        "note: no government documents were downloaded; "
        "robots.txt may have been fetched for policy checks."
    )
    if not plan.entries:
        print("No approved sources found. Approve sources before planning.")
        return 0

    for entry in plan.entries:
        robots_bit = ""
        if entry.robots is not None:
            robots_bit = f" | {entry.robots.summary}"
        delay_bit = (
            f" delay={entry.crawl_delay_seconds}s"
            if entry.crawl_delay_seconds is not None
            else ""
        )
        print(
            f"  - [{entry.decision}] id={entry.source_id} "
            f"{entry.source_name}: {entry.seed_url}{delay_bit}{robots_bit}"
        )
        if entry.notes:
            print(f"      {entry.notes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
