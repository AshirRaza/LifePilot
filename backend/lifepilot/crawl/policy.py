"""Fetch policy defaults for LifePilot crawl planning.

These values describe intended behavior for a future fetcher. The planner
never downloads government documents; it only prepares a dry-run plan.
"""

from __future__ import annotations

from dataclasses import dataclass


CRAWL_PLANNER_USER_AGENT = (
    "LifePilotCrawlPlanner/0.1 "
    "(+https://github.com/AshirRaza/LifePilot; "
    "dry-run crawl planning and robots.txt checks only; "
    "does not download government documents)"
)


@dataclass(frozen=True)
class CrawlPolicy:
    """Conservative defaults for approved-source fetch planning."""

    user_agent: str = CRAWL_PLANNER_USER_AGENT
    # Default pause between requests to the same host when robots.txt
    # does not specify Crawl-delay.
    default_crawl_delay_seconds: float = 2.0
    # Hard ceiling even if robots.txt requests a smaller delay.
    min_crawl_delay_seconds: float = 1.0
    # Soft cap for planning/reporting (enforced later by a real fetcher).
    max_requests_per_host_per_minute: int = 20
    # If robots.txt cannot be retrieved, do not treat the URL as fetchable.
    require_robots_ok: bool = True
