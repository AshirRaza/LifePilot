"""Crawl planning, seed HTML fetch, and bounded in-scope link discovery."""

from lifepilot.crawl.discover import DiscoveryReport, discover_from_document
from lifepilot.crawl.fetcher import SeedFetchReport, SeedFetchResult, fetch_approved_seeds
from lifepilot.crawl.planner import CrawlPlan, CrawlPlanEntry, build_crawl_plan
from lifepilot.crawl.policy import CrawlPolicy, fetcher_policy

__all__ = [
    "CrawlPlan",
    "CrawlPlanEntry",
    "CrawlPolicy",
    "DiscoveryReport",
    "SeedFetchReport",
    "SeedFetchResult",
    "build_crawl_plan",
    "discover_from_document",
    "fetch_approved_seeds",
    "fetcher_policy",
]
