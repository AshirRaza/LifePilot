"""Phase 6D.2 discovery frontier + BFS resume tests (mocked HTTP)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import delete, func, select

from lifepilot.crawl.fetcher import (
    STATUS_FAILED,
    STATUS_FETCHED_NEW_VERSION,
    STATUS_UNCHANGED,
    SeedFetchResult,
)
from lifepilot.crawl.frontier import (
    claim_pending,
    count_pending_eligible,
    enqueue_seed,
    enqueue_url,
    mark_transient_failure,
)
from lifepilot.crawl.frontier_crawl import classify_fetch_failure, crawl_source_frontier
from lifepilot.crawl.policy import fetcher_policy
from lifepilot.crawl.storage import write_bytes_atomic
from lifepilot.db.models import (
    Authority,
    DiscoveryFrontier,
    Document,
    DocumentContentComparison,
    DocumentExtraction,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.ingest.pipeline import (
    IngestLimits,
    MODE_PROCESS_EXISTING,
    _set_coverage_status,
    run_ingestion,
)
from lifepilot.ingest.pipeline import IngestRunReport


def _clear(session) -> None:
    session.execute(delete(DocumentContentComparison))
    session.execute(delete(DocumentExtraction))
    session.execute(delete(DiscoveryFrontier))
    session.execute(delete(DocumentVersion))
    session.execute(delete(Document))
    session.execute(delete(Source))
    session.execute(delete(Authority))
    session.execute(delete(Jurisdiction))
    session.flush()


def _seed_source(session, *, name: str = "gov") -> Source:
    country = Jurisdiction(
        code=f"XX-{name[:8]}",
        name="Example",
        jurisdiction_type="country",
        country_code="XX",
    )
    session.add(country)
    session.flush()
    authority = Authority(jurisdiction_id=country.id, name=f"Auth {name}")
    session.add(authority)
    session.flush()
    seed = "https://www.example.test/guidance/visa"
    source = Source(
        authority_id=authority.id,
        jurisdiction_id=country.id,
        name=name,
        seed_url=seed,
        seed_url_normalized=seed,
        allowed_url_patterns=[f"{seed}*"],
        review_status="approved",
    )
    session.add(source)
    session.flush()
    return source


def test_enqueue_idempotent_and_out_of_scope(db_session):
    _clear(db_session)
    source = _seed_source(db_session)
    first = enqueue_url(
        db_session,
        source,
        url="https://www.example.test/guidance/visa/eligibility",
        discovery_method="html_link",
        depth=1,
        parent_url=source.seed_url_normalized,
    )
    assert first.outcome == "inserted_pending"
    second = enqueue_url(
        db_session,
        source,
        url="https://www.example.test/guidance/visa/eligibility",
        discovery_method="html_link",
        depth=2,
        parent_url=source.seed_url_normalized,
    )
    assert second.outcome == "unchanged"
    assert second.row is not None
    assert second.row.depth == 1  # first-seen wins

    oos = enqueue_url(
        db_session,
        source,
        url="https://other.test/page",
        discovery_method="html_link",
        depth=1,
    )
    assert oos.outcome == "inserted_out_of_scope"
    assert oos.row is not None
    assert oos.row.status == "out_of_scope"


def test_trap_urls_skipped_not_pending(db_session):
    _clear(db_session)
    source = _seed_source(db_session)
    result = enqueue_url(
        db_session,
        source,
        url="https://www.example.test/guidance/visa/search?q=1",
        discovery_method="html_link",
        depth=1,
    )
    assert result.outcome == "inserted_skipped"
    assert result.row is not None
    assert result.row.status == "skipped"


def test_bfs_claim_order(db_session):
    _clear(db_session)
    source = _seed_source(db_session)
    enqueue_seed(db_session, source)
    enqueue_url(
        db_session,
        source,
        url="https://www.example.test/guidance/visa/b",
        discovery_method="html_link",
        depth=2,
    )
    enqueue_url(
        db_session,
        source,
        url="https://www.example.test/guidance/visa/a",
        discovery_method="html_link",
        depth=1,
    )
    rows = claim_pending(db_session, source.id, limit=10, max_depth=3)
    depths = [r.depth for r in rows]
    assert depths == sorted(depths)
    assert rows[0].discovery_method == "seed"


def test_transient_failure_keeps_pending_with_backoff(db_session):
    _clear(db_session)
    source = _seed_source(db_session)
    row = enqueue_seed(db_session, source).row
    assert row is not None
    status = mark_transient_failure(
        db_session,
        row,
        reason="HTTP 503",
        max_attempts=3,
        base_backoff_seconds=30.0,
        retry_after_seconds=120.0,
    )
    assert status == "pending"
    assert row.status == "pending"
    assert row.failure_class == "transient"
    assert row.next_attempt_at is not None
    assert row.next_attempt_at > datetime.now(timezone.utc) + timedelta(seconds=60)
    # Not due yet → claim empty
    due = claim_pending(db_session, source.id, limit=5, max_depth=3)
    assert due == []
    # Force due
    row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.flush()
    due2 = claim_pending(db_session, source.id, limit=5, max_depth=3)
    assert len(due2) == 1


def test_classify_retry_after_statuses():
    transient, _ = classify_fetch_failure(
        SeedFetchResult(
            source_id=1,
            source_name="x",
            seed_url="https://example.test/a",
            status=STATUS_FAILED,
            http_status=429,
            message="HTTP 429",
            retry_after_seconds=30.0,
        )
    )
    assert transient == "transient"
    blocked, _ = classify_fetch_failure(
        SeedFetchResult(
            source_id=1,
            source_name="x",
            seed_url="https://example.test/a",
            status=STATUS_FAILED,
            message="robots.txt disallows fetch for this URL",
        )
    )
    assert blocked == "blocked"


def test_unchanged_html_still_expands_children(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    enqueue_seed(db_session, source)

    html = b"""<!DOCTYPE html><html><body>
    <a href="/guidance/visa/child">Child page</a>
    </body></html>"""

    def fake_fetch(session, src, *, url, **kwargs):
        # Persist a document + version once, then report unchanged.
        from lifepilot.db.models import Document, DocumentVersion

        existing = session.scalar(
            select(Document).where(Document.url_normalized == url)
        )
        if existing is None:
            doc = Document(
                authority_id=src.authority_id,
                primary_source_id=src.id,
                canonical_url=url,
                url_normalized=url,
                content_kind="html_page",
                discovery_status="seed",
            )
            session.add(doc)
            session.flush()
            rel = f"raw/documents/{doc.id}/v1.html"
            write_bytes_atomic(tmp_path / rel, html)
            ver = DocumentVersion(
                document_id=doc.id,
                fetched_via_source_id=src.id,
                version_number=1,
                source_url=url,
                content_type="text/html",
                retrieved_at=datetime.now(timezone.utc),
                http_status=200,
                content_sha256="a" * 64,
                byte_size=len(html),
                storage_path=rel,
                is_current=True,
            )
            session.add(ver)
            session.flush()
            return SeedFetchResult(
                source_id=src.id,
                source_name=src.name,
                seed_url=url,
                status=STATUS_UNCHANGED,
                document_id=doc.id,
                message="unchanged",
            )
        return SeedFetchResult(
            source_id=src.id,
            source_name=src.name,
            seed_url=url,
            status=STATUS_UNCHANGED,
            document_id=existing.id,
            message="unchanged",
        )

    policy = fetcher_policy(max_depth=2, max_sitemap_fetches_per_source=0)
    network = [5]
    with patch(
        "lifepilot.crawl.frontier_crawl.fetch_html_document_url",
        side_effect=fake_fetch,
    ):
        with patch(
            "lifepilot.crawl.frontier_crawl.discover_sitemaps_for_source"
        ) as sm:
            sm.return_value = MagicMock(
                sitemap_fetches_attempted=0,
                sitemap_fetches_ok=0,
                urls_enqueued_pending=0,
                notes=[],
            )
            report = crawl_source_frontier(
                db_session,
                source,
                policy=policy,
                http_client=MagicMock(),
                storage_root=tmp_path,
                network_budget=network,
                max_html=1,  # seed only; child stays pending
                max_binary=0,
                host_times={},
                enable_sitemaps=True,
            )
    assert report.unchanged >= 1
    assert report.children_enqueued >= 1
    child = db_session.scalar(
        select(DiscoveryFrontier).where(
            DiscoveryFrontier.url_normalized
            == "https://www.example.test/guidance/visa/child"
        )
    )
    assert child is not None
    assert child.status == "pending"
    assert child.depth == 1


def test_coverage_not_complete_with_pending_or_limits():
    report = IngestRunReport(started_at="t0")
    report.frontier_pending_remaining = 3
    report.extraction_backlog_remaining = 0
    report.limits_reached = ["max_fetches"]
    _set_coverage_status(report)
    assert report.complete_within_approved_scope is False
    assert report.coverage_status == "budget_limited"

    report2 = IngestRunReport(started_at="t0")
    report2.frontier_pending_remaining = 0
    report2.extraction_backlog_remaining = 0
    report2.depth_capped_expansions = 2
    report2.limits_reached = ["max_depth"]
    _set_coverage_status(report2)
    assert report2.complete_within_approved_scope is False
    assert report2.coverage_status == "depth_limited"

    report3 = IngestRunReport(started_at="t0")
    report3.frontier_pending_remaining = 0
    report3.extraction_backlog_remaining = 0
    report3.depth_capped_expansions = 0
    report3.limits_reached = []
    _set_coverage_status(report3)
    assert report3.complete_within_approved_scope is True
    assert report3.coverage_status == "exhausted_within_scope"


def test_dry_run_does_not_write_frontier(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    before = db_session.scalar(select(func.count()).select_from(DiscoveryFrontier))
    run_ingestion(
        db_session,
        execute=False,
        source_id=source.id,
        storage_root=tmp_path,
        write_report_file=False,
    )
    after = db_session.scalar(select(func.count()).select_from(DiscoveryFrontier))
    assert before == after == 0
