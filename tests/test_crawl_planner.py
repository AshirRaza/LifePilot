"""Tests for dry-run crawl planning (no document downloads)."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
from sqlalchemy import delete, select

from lifepilot.crawl.planner import (
    DECISION_BLOCKED_BY_ROBOTS,
    DECISION_OUT_OF_SCOPE,
    DECISION_PLANNED,
    DECISION_ROBOTS_UNAVAILABLE,
    build_crawl_plan,
)
from lifepilot.crawl.policy import CrawlPolicy
from lifepilot.crawl.robots import check_robots, effective_crawl_delay, robots_url_for
from lifepilot.db.models import (
    Authority,
    Document,
    DocumentExtraction,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.registry.importer import import_seed_sources
from lifepilot.registry.review import set_source_review_status


def _clear_registry(session) -> None:
    session.execute(delete(DocumentExtraction))
    session.execute(delete(DocumentVersion))
    session.execute(delete(Document))
    session.execute(delete(Source))
    session.execute(delete(Authority))
    session.execute(delete(Jurisdiction))
    session.flush()


def _mock_robots_client(body: str, status_code: int = 200) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.status_code = status_code
    response.text = body
    client.get.return_value = response
    return client


def test_robots_url_for_origin():
    assert (
        robots_url_for("https://www.gov.uk/skilled-worker-visa")
        == "https://www.gov.uk/robots.txt"
    )


def test_robots_allows_and_respects_crawl_delay():
    body = """User-agent: *
Crawl-delay: 5
Allow: /
"""
    client = _mock_robots_client(body)
    policy = CrawlPolicy()
    decision = check_robots(
        "https://www.gov.uk/skilled-worker-visa",
        policy,
        client=client,
    )
    assert decision.fetched is True
    assert decision.can_fetch is True
    assert decision.crawl_delay_seconds == 5.0
    assert effective_crawl_delay(decision, policy) == 5.0
    client.get.assert_called()
    assert "robots.txt" in client.get.call_args.args[0]


def test_robots_disallow_blocks_agent():
    body = """User-agent: LifePilotCrawlPlanner
Disallow: /
"""
    client = _mock_robots_client(body)
    decision = check_robots(
        "https://www.gov.uk/skilled-worker-visa",
        CrawlPolicy(),
        client=client,
    )
    assert decision.can_fetch is False


def test_plan_only_includes_approved_sources(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()

    # Approve exactly one source.
    source = db_session.scalar(
        select(Source).where(
            Source.seed_url_normalized == "https://www.gov.uk/skilled-worker-visa"
        )
    )
    assert source is not None
    set_source_review_status(db_session, source_id=source.id, status="approved")
    db_session.flush()

    client = _mock_robots_client("User-agent: *\nAllow: /\n")
    plan = build_crawl_plan(db_session, http_client=client)
    assert len(plan.entries) == 1
    assert plan.entries[0].source_id == source.id
    assert plan.entries[0].decision == DECISION_PLANNED
    assert plan.entries[0].would_fetch is True


def test_plan_blocked_by_robots(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(select(Source).limit(1))
    assert source is not None
    set_source_review_status(db_session, source_id=source.id, status="approved")
    db_session.flush()

    client = _mock_robots_client("User-agent: *\nDisallow: /\n")
    plan = build_crawl_plan(db_session, http_client=client)
    assert plan.entries[0].decision == DECISION_BLOCKED_BY_ROBOTS
    assert plan.entries[0].would_fetch is False


def test_plan_robots_unavailable_is_not_planned(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(select(Source).limit(1))
    assert source is not None
    set_source_review_status(db_session, source_id=source.id, status="approved")
    db_session.flush()

    client = MagicMock()
    client.get.side_effect = httpx.ConnectError("offline")
    plan = build_crawl_plan(db_session, http_client=client)
    assert plan.entries[0].decision == DECISION_ROBOTS_UNAVAILABLE
    assert plan.planned_count == 0


def test_plan_marks_out_of_scope_seed(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(
        select(Source).where(
            Source.seed_url_normalized == "https://www.gov.uk/skilled-worker-visa"
        )
    )
    assert source is not None
    set_source_review_status(db_session, source_id=source.id, status="approved")
    # Simulate a misconfigured allowlist that no longer covers the seed.
    source.allowed_url_patterns = ["https://www.gov.uk/some-other-path*"]
    db_session.flush()

    client = _mock_robots_client("User-agent: *\nAllow: /\n")
    plan = build_crawl_plan(db_session, http_client=client)
    assert plan.entries[0].decision == DECISION_OUT_OF_SCOPE


def test_planner_does_not_create_documents(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(select(Source).limit(1))
    assert source is not None
    set_source_review_status(db_session, source_id=source.id, status="approved")
    db_session.flush()

    client = _mock_robots_client("User-agent: *\nAllow: /\n")
    build_crawl_plan(db_session, http_client=client)
    db_session.flush()
    assert db_session.scalar(select(Document).limit(1)) is None


def test_transparent_planner_user_agent():
    policy = CrawlPolicy()
    assert "LifePilotCrawlPlanner" in policy.user_agent
    assert "Mozilla" not in policy.user_agent
