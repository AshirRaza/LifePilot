"""Fetch policy defaults for LifePilot crawl planning and seed fetching."""

from __future__ import annotations

from dataclasses import dataclass


CRAWL_PLANNER_USER_AGENT = (
    "LifePilotCrawlPlanner/0.1 "
    "(+https://github.com/AshirRaza/LifePilot; "
    "dry-run crawl planning and robots.txt checks only; "
    "does not download government documents)"
)

FETCHER_USER_AGENT = (
    "LifePilotFetcher/0.1 "
    "(+https://github.com/AshirRaza/LifePilot; "
    "approved-seed and bounded in-scope HTML fetch only)"
)


@dataclass(frozen=True)
class CrawlPolicy:
    """Conservative defaults for approved-source planning and fetching."""

    user_agent: str = CRAWL_PLANNER_USER_AGENT
    # Default pause between requests to the same host when robots.txt
    # does not specify Crawl-delay.
    default_crawl_delay_seconds: float = 2.0
    # Hard ceiling even if robots.txt requests a smaller delay.
    min_crawl_delay_seconds: float = 1.0
    # Soft cap for planning/reporting; fetcher also paces by crawl delay.
    max_requests_per_host_per_minute: int = 20
    # If robots.txt cannot be retrieved, do not treat the URL as fetchable.
    require_robots_ok: bool = True
    # Network / response limits for the HTML seed fetcher.
    request_timeout_seconds: float = 30.0
    max_response_bytes: int = 5_000_000
    max_redirects: int = 5
    # Bounded discovery caps (Phase 4C / 4D).
    max_discovered_links_listed: int = 200
    max_discovered_html_fetches_per_run: int = 25
    max_discovered_binary_fetches_per_run: int = 3
    # Binary PDF/DOCX download limits (Phase 4D).
    max_binary_response_bytes: int = 20_000_000
    max_docx_uncompressed_bytes: int = 80_000_000
    binary_request_timeout_seconds: float = 60.0


def fetcher_policy(
    *,
    default_crawl_delay_seconds: float = 2.0,
    min_crawl_delay_seconds: float = 1.0,
    max_requests_per_host_per_minute: int = 20,
    require_robots_ok: bool = True,
    request_timeout_seconds: float = 30.0,
    max_response_bytes: int = 5_000_000,
    max_redirects: int = 5,
    max_discovered_links_listed: int = 200,
    max_discovered_html_fetches_per_run: int = 25,
    max_discovered_binary_fetches_per_run: int = 3,
    max_binary_response_bytes: int = 20_000_000,
    max_docx_uncompressed_bytes: int = 80_000_000,
    binary_request_timeout_seconds: float = 60.0,
) -> CrawlPolicy:
    """Policy for real HTML/binary fetches (transparent fetcher user-agent)."""
    return CrawlPolicy(
        user_agent=FETCHER_USER_AGENT,
        default_crawl_delay_seconds=default_crawl_delay_seconds,
        min_crawl_delay_seconds=min_crawl_delay_seconds,
        max_requests_per_host_per_minute=max_requests_per_host_per_minute,
        require_robots_ok=require_robots_ok,
        request_timeout_seconds=request_timeout_seconds,
        max_response_bytes=max_response_bytes,
        max_redirects=max_redirects,
        max_discovered_links_listed=max_discovered_links_listed,
        max_discovered_html_fetches_per_run=max_discovered_html_fetches_per_run,
        max_discovered_binary_fetches_per_run=max_discovered_binary_fetches_per_run,
        max_binary_response_bytes=max_binary_response_bytes,
        max_docx_uncompressed_bytes=max_docx_uncompressed_bytes,
        binary_request_timeout_seconds=binary_request_timeout_seconds,
    )
