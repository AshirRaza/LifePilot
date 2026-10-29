"""robots.txt checks for crawl planning.

Fetches only ``/robots.txt`` (not page content). A successful check is not
permission to crawl at scale — site terms and rate limits still apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import certifi
import httpx

from lifepilot.crawl.policy import CrawlPolicy


@dataclass(frozen=True)
class RobotsDecision:
    robots_url: str
    fetched: bool
    status_code: int | None
    can_fetch: bool | None
    crawl_delay_seconds: float | None
    error: str | None = None
    sitemap_urls: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        if not self.fetched:
            return f"robots_unfetched error={self.error}"
        return (
            f"robots_status={self.status_code} can_fetch={self.can_fetch} "
            f"crawl_delay={self.crawl_delay_seconds} "
            f"sitemaps={len(self.sitemap_urls)}"
        )


def parse_sitemap_directives(robots_body: str) -> list[str]:
    """Extract ``Sitemap:`` URLs from a robots.txt body (order preserved)."""
    found: list[str] = []
    seen: set[str] = set()
    for raw_line in robots_body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() != "sitemap":
            continue
        url = value.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        found.append(url)
    return found


def robots_url_for(page_url: str) -> str:
    parts = urlsplit(page_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"Cannot derive robots.txt URL from {page_url!r}")
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


def _parse_robots(robots_url: str, body: str) -> RobotFileParser:
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(body.splitlines())
    return parser


def check_robots(
    page_url: str,
    policy: CrawlPolicy,
    *,
    client: httpx.Client | None = None,
) -> RobotsDecision:
    """Fetch and evaluate robots.txt for ``page_url`` under ``policy.user_agent``."""
    robots_url = robots_url_for(page_url)
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            verify=certifi.where(),
            headers={"User-Agent": policy.user_agent, "Accept": "text/plain,*/*;q=0.8"},
        )

    try:
        # Stream/read robots.txt only — small policy file, not document content.
        response = client.get(robots_url)
        if response.status_code == 404:
            # No robots.txt → conventional allow, still subject to our rate policy.
            return RobotsDecision(
                robots_url=robots_url,
                fetched=True,
                status_code=404,
                can_fetch=True,
                crawl_delay_seconds=None,
                sitemap_urls=(),
            )
        if response.status_code >= 400:
            return RobotsDecision(
                robots_url=robots_url,
                fetched=False,
                status_code=response.status_code,
                can_fetch=None,
                crawl_delay_seconds=None,
                error=f"HTTP {response.status_code}",
            )

        body = response.text
        parser = _parse_robots(robots_url, body)
        can_fetch = parser.can_fetch(policy.user_agent, page_url)
        delay = parser.crawl_delay(policy.user_agent)
        sitemaps = tuple(parse_sitemap_directives(body))
        return RobotsDecision(
            robots_url=robots_url,
            fetched=True,
            status_code=response.status_code,
            can_fetch=can_fetch,
            crawl_delay_seconds=float(delay) if delay is not None else None,
            sitemap_urls=sitemaps,
        )
    except httpx.HTTPError as exc:
        return RobotsDecision(
            robots_url=robots_url,
            fetched=False,
            status_code=None,
            can_fetch=None,
            crawl_delay_seconds=None,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if owns_client:
            client.close()


def effective_crawl_delay(
    decision: RobotsDecision,
    policy: CrawlPolicy,
) -> float:
    """Resolve crawl delay from robots.txt and local policy floors/defaults."""
    if decision.crawl_delay_seconds is not None:
        return max(decision.crawl_delay_seconds, policy.min_crawl_delay_seconds)
    return max(policy.default_crawl_delay_seconds, policy.min_crawl_delay_seconds)
