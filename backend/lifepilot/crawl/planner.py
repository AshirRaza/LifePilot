"""Dry-run crawl planner for approved registry sources.

Plans seed URL fetches only. Does not download pages, follow links, or write
``documents`` / ``document_versions`` rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.crawl.policy import CrawlPolicy
from lifepilot.crawl.robots import (
    RobotsDecision,
    check_robots,
    effective_crawl_delay,
)
from lifepilot.db.models import Source
from lifepilot.registry.urls import url_in_any_allowed_pattern


# Plan entry decisions (not review_status).
DECISION_PLANNED = "planned"
DECISION_BLOCKED_BY_ROBOTS = "blocked_by_robots"
DECISION_ROBOTS_UNAVAILABLE = "robots_unavailable"
DECISION_OUT_OF_SCOPE = "out_of_scope"
DECISION_INVALID_SOURCE = "invalid_source"


@dataclass
class CrawlPlanEntry:
    source_id: int
    source_name: str
    seed_url: str
    seed_url_normalized: str
    decision: str
    in_allowlist: bool
    crawl_delay_seconds: float | None
    max_requests_per_host_per_minute: int
    robots: RobotsDecision | None = None
    notes: str = ""

    @property
    def would_fetch(self) -> bool:
        return self.decision == DECISION_PLANNED


@dataclass
class CrawlPlan:
    policy: CrawlPolicy
    entries: list[CrawlPlanEntry] = field(default_factory=list)

    @property
    def planned_count(self) -> int:
        return sum(1 for e in self.entries if e.decision == DECISION_PLANNED)

    @property
    def blocked_count(self) -> int:
        return sum(
            1
            for e in self.entries
            if e.decision
            in {
                DECISION_BLOCKED_BY_ROBOTS,
                DECISION_ROBOTS_UNAVAILABLE,
                DECISION_OUT_OF_SCOPE,
                DECISION_INVALID_SOURCE,
            }
        )


def _load_approved_sources(
    session: Session,
    *,
    source_id: int | None = None,
) -> list[Source]:
    stmt = select(Source).where(Source.review_status == "approved").order_by(Source.id)
    if source_id is not None:
        stmt = stmt.where(Source.id == source_id)
    return list(session.scalars(stmt).all())


def evaluate_source(
    source: Source,
    policy: CrawlPolicy,
    *,
    client=None,
) -> CrawlPlanEntry:
    """Recheck approval preconditions for a single source (seed URL only)."""
    if source.review_status != "approved":
        return CrawlPlanEntry(
            source_id=source.id,
            source_name=source.name,
            seed_url=source.seed_url or "",
            seed_url_normalized=source.seed_url_normalized or "",
            decision=DECISION_INVALID_SOURCE,
            in_allowlist=False,
            crawl_delay_seconds=None,
            max_requests_per_host_per_minute=policy.max_requests_per_host_per_minute,
            notes=f"source review_status={source.review_status!r}; not approved",
        )
    return _decide_entry(source, policy, client=client)


def _decide_entry(
    source: Source,
    policy: CrawlPolicy,
    *,
    client=None,
) -> CrawlPlanEntry:
    seed_url = source.seed_url
    patterns = list(source.allowed_url_patterns or [])
    normalized = source.seed_url_normalized

    if not seed_url or not normalized or not patterns:
        return CrawlPlanEntry(
            source_id=source.id,
            source_name=source.name,
            seed_url=seed_url or "",
            seed_url_normalized=normalized or "",
            decision=DECISION_INVALID_SOURCE,
            in_allowlist=False,
            crawl_delay_seconds=None,
            max_requests_per_host_per_minute=policy.max_requests_per_host_per_minute,
            notes="missing seed URL or allowlist patterns",
        )

    in_allowlist = url_in_any_allowed_pattern(seed_url, patterns)
    if not in_allowlist:
        return CrawlPlanEntry(
            source_id=source.id,
            source_name=source.name,
            seed_url=seed_url,
            seed_url_normalized=normalized,
            decision=DECISION_OUT_OF_SCOPE,
            in_allowlist=False,
            crawl_delay_seconds=None,
            max_requests_per_host_per_minute=policy.max_requests_per_host_per_minute,
            notes="seed URL is outside allowed_url_patterns",
        )

    robots = check_robots(seed_url, policy, client=client)
    delay = effective_crawl_delay(robots, policy)

    if not robots.fetched:
        decision = (
            DECISION_ROBOTS_UNAVAILABLE
            if policy.require_robots_ok
            else DECISION_PLANNED
        )
        return CrawlPlanEntry(
            source_id=source.id,
            source_name=source.name,
            seed_url=seed_url,
            seed_url_normalized=normalized,
            decision=decision,
            in_allowlist=True,
            crawl_delay_seconds=delay if decision == DECISION_PLANNED else None,
            max_requests_per_host_per_minute=policy.max_requests_per_host_per_minute,
            robots=robots,
            notes="robots.txt could not be retrieved; fetch not planned",
        )

    if robots.can_fetch is False:
        return CrawlPlanEntry(
            source_id=source.id,
            source_name=source.name,
            seed_url=seed_url,
            seed_url_normalized=normalized,
            decision=DECISION_BLOCKED_BY_ROBOTS,
            in_allowlist=True,
            crawl_delay_seconds=None,
            max_requests_per_host_per_minute=policy.max_requests_per_host_per_minute,
            robots=robots,
            notes="robots.txt disallows this user-agent for the seed URL",
        )

    return CrawlPlanEntry(
        source_id=source.id,
        source_name=source.name,
        seed_url=seed_url,
        seed_url_normalized=normalized,
        decision=DECISION_PLANNED,
        in_allowlist=True,
        crawl_delay_seconds=delay,
        max_requests_per_host_per_minute=policy.max_requests_per_host_per_minute,
        robots=robots,
        notes=(
            "dry-run only: seed URL would be eligible for a future fetcher; "
            "no document download performed"
        ),
    )


def build_crawl_plan(
    session: Session,
    *,
    policy: CrawlPolicy | None = None,
    http_client=None,
    source_id: int | None = None,
) -> CrawlPlan:
    """Build a dry-run plan for approved sources (seed URLs only)."""
    policy = policy or CrawlPolicy()
    plan = CrawlPlan(policy=policy)
    for source in _load_approved_sources(session, source_id=source_id):
        plan.entries.append(evaluate_source(source, policy, client=http_client))
    return plan
