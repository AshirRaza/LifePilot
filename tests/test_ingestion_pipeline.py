"""Phase 6A unified ingestion pipeline tests (mocked; no live government I/O)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import delete, func, select

from lifepilot.crawl.fetcher import (
    STATUS_FETCHED_NEW_VERSION,
    STATUS_UNCHANGED,
    SeedFetchResult,
)
from lifepilot.crawl.frontier_crawl import FrontierCrawlReport
from lifepilot.crawl.planner import DECISION_PLANNED
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
from lifepilot.extract.runner import extract_document_version
from lifepilot.extract.runner import EXTRACTORS
from lifepilot.ingest.pipeline import (
    MODE_PROCESS_EXISTING,
    IngestLimits,
    run_ingestion,
)


SAMPLE_HTML = b"""<!DOCTYPE html><html><head><title>Visa</title></head>
<body><main id="content"><h1>Visa</h1><p>Eligible workers may apply.</p></main></body></html>"""

SAMPLE_HTML_V2 = b"""<!DOCTYPE html><html><head><title>Visa</title></head>
<body><main id="content"><h1>Visa</h1><p>Eligible workers may apply for 6 months.</p></main></body></html>"""


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


def _seed_source(session, *, status: str = "approved", name: str = "ingest_src") -> Source:
    slug = "".join(ch if ch.isalnum() else "-" for ch in name)[:24] or "src"
    country = Jurisdiction(
        code=f"GB-{slug}"[:32],
        name=f"United Kingdom ({slug})",
        jurisdiction_type="country",
        country_code="GB",
    )
    session.add(country)
    session.flush()
    authority = Authority(jurisdiction_id=country.id, name=f"Home Office {slug}")
    session.add(authority)
    session.flush()
    seed = f"https://www.gov.uk/ingest-test-{slug}"
    source = Source(
        jurisdiction_id=country.id,
        authority_id=authority.id,
        name=name,
        seed_url=seed,
        seed_url_normalized=seed,
        allowed_url_patterns=[f"{seed}*"],
        default_language="en",
        review_status=status,
    )
    session.add(source)
    session.flush()
    return source


def _add_doc_version(
    session,
    tmp_path: Path,
    source: Source,
    *,
    url: str,
    version_number: int,
    html: bytes,
    raw_sha: str,
    is_current: bool,
) -> tuple[Document, DocumentVersion]:
    document = session.scalar(
        select(Document).where(Document.url_normalized == url)
    )
    if document is None:
        document = Document(
            authority_id=source.authority_id,
            primary_source_id=source.id,
            canonical_url=url,
            url_normalized=url,
            content_kind="html_page",
            language="en",
            discovery_status="seed",
        )
        session.add(document)
        session.flush()
    rel = f"raw/documents/{document.id}/v{version_number}.html"
    write_bytes_atomic(tmp_path / rel, html)
    version = DocumentVersion(
        document_id=document.id,
        fetched_via_source_id=source.id,
        version_number=version_number,
        source_url=url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime.now(timezone.utc),
        http_status=200,
        content_sha256=raw_sha,
        byte_size=len(html),
        storage_path=rel,
        is_current=is_current,
    )
    session.add(version)
    session.flush()
    return document, version


def test_dry_run_no_network_or_writes(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    client = MagicMock()
    report = run_ingestion(
        db_session,
        execute=False,
        source_id=source.id,
        limits=IngestLimits(),
        http_client=client,
        storage_root=tmp_path,
        write_report_file=True,
    )
    assert report.execute is False
    assert report.completed is True
    assert report.selected_sources
    assert report.selected_sources[0]["source_id"] == source.id
    assert "would_fetch_seeds" in report.planned
    client.get.assert_not_called()
    client.stream.assert_not_called()
    assert db_session.scalar(select(func.count()).select_from(Document)) == 0
    assert list(tmp_path.rglob("*")) == [] or not any(
        p.suffix == ".json" for p in tmp_path.rglob("*")
    )


def test_candidate_source_excluded(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session, status="candidate")
    report = run_ingestion(
        db_session,
        execute=False,
        source_id=source.id,
        storage_root=tmp_path,
    )
    assert report.selected_sources == []
    assert any("not approved" in e for e in report.exclusions)


def test_global_max_sources_limit(db_session, tmp_path: Path):
    _clear(db_session)
    _seed_source(db_session, name="a")
    _seed_source(db_session, name="b")
    report = run_ingestion(
        db_session,
        execute=False,
        limits=IngestLimits(max_sources=1),
        storage_root=tmp_path,
    )
    assert len(report.selected_sources) == 1
    assert "max_sources" in report.limits_reached


def test_execute_respects_fetch_budget_and_isolates_failures(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)

    with patch("lifepilot.ingest.pipeline.evaluate_source") as ev:
        ev.return_value = MagicMock(decision=DECISION_PLANNED, notes="ok")
        with patch(
            "lifepilot.ingest.pipeline.crawl_source_frontier",
            side_effect=RuntimeError("frontier boom"),
        ):
            report = run_ingestion(
                db_session,
                execute=True,
                source_id=source.id,
                limits=IngestLimits(
                    max_fetches=2,
                    max_extractions=0,
                    max_comparisons=0,
                ),
                storage_root=tmp_path,
                write_report_file=False,
            )
    assert any(f.stage == "frontier_crawl" for f in report.failures)
    assert report.ended_at is not None


def test_execute_skips_existing_compatible_extraction(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    doc, version = _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=source.seed_url_normalized,
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="1" * 64,
        is_current=True,
    )
    result = extract_document_version(
        db_session, version, doc, persist=True, storage_root=tmp_path
    )
    assert result.status == "success"
    db_session.flush()

    empty_crawl = FrontierCrawlReport(source_id=source.id)
    with patch("lifepilot.ingest.pipeline.evaluate_source") as ev:
        ev.return_value = MagicMock(decision=DECISION_PLANNED, notes="ok")
        with patch(
            "lifepilot.ingest.pipeline.crawl_source_frontier",
            return_value=empty_crawl,
        ):
            report = run_ingestion(
                db_session,
                execute=True,
                source_id=source.id,
                limits=IngestLimits(
                    max_fetches=1,
                    max_extractions=5,
                    max_comparisons=0,
                ),
                storage_root=tmp_path,
                write_report_file=False,
            )
    assert report.extractions_success == 0
    assert report.extractions_failed == 0


def test_missing_extraction_resumed(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    doc, version = _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=source.seed_url_normalized,
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="2" * 64,
        is_current=True,
    )
    crawl = FrontierCrawlReport(source_id=source.id)
    with patch("lifepilot.ingest.pipeline.evaluate_source") as ev:
        ev.return_value = MagicMock(decision=DECISION_PLANNED, notes="ok")
        with patch(
            "lifepilot.ingest.pipeline.crawl_source_frontier",
            return_value=crawl,
        ):
            report = run_ingestion(
                db_session,
                execute=True,
                source_id=source.id,
                limits=IngestLimits(
                    max_fetches=1,
                    max_extractions=5,
                    max_comparisons=0,
                ),
                storage_root=tmp_path,
                write_report_file=False,
            )
    assert report.extractions_success == 1
    assert db_session.scalar(select(func.count()).select_from(DocumentExtraction)) == 1


def test_compare_consecutive_versions(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    doc, v1 = _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=source.seed_url_normalized,
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="a" * 64,
        is_current=False,
    )
    _, v2 = _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=source.seed_url_normalized,
        version_number=2,
        html=SAMPLE_HTML_V2,
        raw_sha="b" * 64,
        is_current=True,
    )
    extract_document_version(db_session, v1, doc, persist=True, storage_root=tmp_path)
    extract_document_version(db_session, v2, doc, persist=True, storage_root=tmp_path)
    db_session.flush()

    crawl = FrontierCrawlReport(source_id=source.id)
    with patch("lifepilot.ingest.pipeline.evaluate_source") as ev:
        ev.return_value = MagicMock(decision=DECISION_PLANNED, notes="ok")
        with patch(
            "lifepilot.ingest.pipeline.crawl_source_frontier",
            return_value=crawl,
        ):
            report = run_ingestion(
                db_session,
                execute=True,
                source_id=source.id,
                limits=IngestLimits(
                    max_fetches=1,
                    max_extractions=0,
                    max_comparisons=5,
                ),
                storage_root=tmp_path,
                write_report_file=False,
            )
    assert report.comparisons_changed == 1
    assert db_session.scalar(select(func.count()).select_from(DocumentContentComparison)) == 1


def test_extraction_json_restored_on_db_failure(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    doc, version = _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=source.seed_url_normalized,
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="c" * 64,
        is_current=True,
    )
    first = extract_document_version(
        db_session, version, doc, persist=True, storage_root=tmp_path
    )
    assert first.status == "success"
    original = (tmp_path / first.storage_path).read_bytes()

    # Force a re-extract path by bumping stored extractor version in DB only.
    row = db_session.scalar(select(DocumentExtraction))
    assert row is not None
    row.extractor_version = "0.0.1"
    db_session.flush()

    with patch.object(db_session, "flush", side_effect=RuntimeError("db down")):
        with pytest.raises(RuntimeError):
            extract_document_version(
                db_session, version, doc, persist=True, storage_root=tmp_path
            )

    # Prior JSON restored.
    assert (tmp_path / first.storage_path).read_bytes() == original
    assert not (tmp_path / (first.storage_path + ".bak")).exists()


def test_dry_run_planned_extractions_count(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=source.seed_url_normalized,
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="d" * 64,
        is_current=True,
    )
    report = run_ingestion(
        db_session,
        execute=False,
        source_id=source.id,
        storage_root=tmp_path,
    )
    assert report.planned["versions_needing_extraction_this_run"] == 1
    assert report.extraction_backlog_remaining == 1


def test_process_existing_makes_no_http(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=source.seed_url_normalized,
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="e" * 64,
        is_current=True,
    )
    client = MagicMock()
    with patch("lifepilot.ingest.pipeline.evaluate_source") as ev:
        with patch("lifepilot.ingest.pipeline.crawl_source_frontier") as crawl:
            report = run_ingestion(
                db_session,
                execute=True,
                mode=MODE_PROCESS_EXISTING,
                source_id=source.id,
                limits=IngestLimits(max_extractions=5, max_comparisons=0),
                http_client=client,
                storage_root=tmp_path,
                write_report_file=False,
            )
    assert report.mode == MODE_PROCESS_EXISTING
    assert report.extractions_success == 1
    assert report.fetches_attempted == 0
    client.get.assert_not_called()
    client.stream.assert_not_called()
    ev.assert_not_called()
    crawl.assert_not_called()


def test_process_existing_dry_run_no_writes(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=source.seed_url_normalized,
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="f" * 64,
        is_current=True,
    )
    before_ext = db_session.scalar(select(func.count()).select_from(DocumentExtraction))
    report = run_ingestion(
        db_session,
        execute=False,
        mode=MODE_PROCESS_EXISTING,
        source_id=source.id,
        storage_root=tmp_path,
    )
    assert report.planned["would_fetch_seeds"] == 0
    assert report.planned["extraction_backlog_total"] == 1
    assert db_session.scalar(select(func.count()).select_from(DocumentExtraction)) == before_ext
    assert not any(p.name.startswith("ingest_") for p in tmp_path.rglob("*.json"))


def test_process_existing_skips_compatible_extraction(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    doc, version = _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=source.seed_url_normalized,
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="11" * 32,
        is_current=True,
    )
    extract_document_version(db_session, version, doc, persist=True, storage_root=tmp_path)
    db_session.flush()
    report = run_ingestion(
        db_session,
        execute=True,
        mode=MODE_PROCESS_EXISTING,
        source_id=source.id,
        limits=IngestLimits(max_extractions=5, max_comparisons=0),
        storage_root=tmp_path,
        write_report_file=False,
    )
    assert report.extractions_success == 0
    assert report.extraction_backlog_remaining == 0


def test_process_existing_incompatible_extractor_reprocessed(
    db_session, tmp_path: Path
):
    _clear(db_session)
    source = _seed_source(db_session)
    doc, version = _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=source.seed_url_normalized,
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="22" * 32,
        is_current=True,
    )
    first = extract_document_version(
        db_session, version, doc, persist=True, storage_root=tmp_path
    )
    assert first.status == "success"
    row = db_session.scalar(select(DocumentExtraction))
    assert row is not None
    row.extractor_version = "0.0.1"
    db_session.flush()

    report = run_ingestion(
        db_session,
        execute=True,
        mode=MODE_PROCESS_EXISTING,
        source_id=source.id,
        limits=IngestLimits(max_extractions=5, max_comparisons=0),
        storage_root=tmp_path,
        write_report_file=False,
    )
    assert report.extractions_success == 1
    refreshed = db_session.scalar(select(DocumentExtraction))
    assert refreshed is not None
    assert refreshed.extractor_version == EXTRACTORS["html_page"].version


def test_all_approved_excludes_candidates(db_session, tmp_path: Path):
    _clear(db_session)
    a = _seed_source(db_session, name="approved-a")
    b = _seed_source(db_session, name="approved-b")
    _seed_source(db_session, status="candidate", name="cand")
    report = run_ingestion(
        db_session,
        execute=False,
        all_approved=True,
        storage_root=tmp_path,
    )
    ids = [s["source_id"] for s in report.selected_sources]
    assert ids == sorted(ids)
    assert set(ids) == {a.id, b.id}
    assert "max_sources" not in report.limits_reached


def test_global_fetch_budget_across_sources(db_session, tmp_path: Path):
    _clear(db_session)
    s1 = _seed_source(db_session, name="budget-a")
    s2 = _seed_source(db_session, name="budget-b")
    crawl_calls: list[int] = []

    def fake_crawl(session, source, **kwargs):
        crawl_calls.append(source.id)
        network_budget = kwargs["network_budget"]
        network_budget[0] = max(0, network_budget[0] - 1)
        report = FrontierCrawlReport(source_id=source.id, stopped_reason="network_budget")
        report.fetch_results.append(
            SeedFetchResult(
                source_id=source.id,
                source_name=source.name,
                seed_url=source.seed_url,
                status=STATUS_UNCHANGED,
                document_id=None,
            )
        )
        return report

    with patch("lifepilot.ingest.pipeline.evaluate_source") as ev:
        ev.return_value = MagicMock(decision=DECISION_PLANNED, notes="ok")
        with patch(
            "lifepilot.ingest.pipeline.crawl_source_frontier",
            side_effect=fake_crawl,
        ):
            report = run_ingestion(
                db_session,
                execute=True,
                all_approved=True,
                limits=IngestLimits(
                    max_sources=10,
                    max_fetches=1,
                    max_extractions=0,
                    max_comparisons=0,
                ),
                storage_root=tmp_path,
                write_report_file=False,
            )
    assert crawl_calls == [s1.id]
    assert report.fetches_attempted == 1
    assert "max_fetches" in report.limits_reached
    stats = {s.source_id: s for s in report.per_source}
    assert stats[s2.id].status == "budget_exhausted"


def test_shared_document_extracted_once(db_session, tmp_path: Path):
    _clear(db_session)
    owner = _seed_source(db_session, name="owner")
    other = _seed_source(db_session, name="other")
    url = "https://www.gov.uk/shared-doc"
    doc, version = _add_doc_version(
        db_session,
        tmp_path,
        owner,
        url=url,
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="33" * 32,
        is_current=True,
    )
    # Provenance via second approved source without changing ownership.
    version.fetched_via_source_id = other.id
    db_session.flush()
    assert doc.primary_source_id == owner.id

    report = run_ingestion(
        db_session,
        execute=True,
        mode=MODE_PROCESS_EXISTING,
        all_approved=True,
        limits=IngestLimits(max_extractions=10, max_comparisons=0),
        storage_root=tmp_path,
        write_report_file=False,
    )
    assert report.extractions_success == 1
    assert db_session.scalar(select(func.count()).select_from(DocumentExtraction)) == 1
    owner_stats = next(s for s in report.per_source if s.source_id == owner.id)
    other_stats = next(s for s in report.per_source if s.source_id == other.id)
    assert owner_stats.extractions_success == 1
    assert other_stats.extractions_success == 0


def test_process_existing_comparison_idempotent(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    doc, v1 = _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=source.seed_url_normalized,
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="44" * 32,
        is_current=False,
    )
    _, v2 = _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=source.seed_url_normalized,
        version_number=2,
        html=SAMPLE_HTML_V2,
        raw_sha="55" * 32,
        is_current=True,
    )
    extract_document_version(db_session, v1, doc, persist=True, storage_root=tmp_path)
    extract_document_version(db_session, v2, doc, persist=True, storage_root=tmp_path)
    db_session.flush()

    r1 = run_ingestion(
        db_session,
        execute=True,
        mode=MODE_PROCESS_EXISTING,
        source_id=source.id,
        limits=IngestLimits(max_extractions=0, max_comparisons=5),
        storage_root=tmp_path,
        write_report_file=False,
    )
    assert r1.comparisons_changed == 1
    r2 = run_ingestion(
        db_session,
        execute=True,
        mode=MODE_PROCESS_EXISTING,
        source_id=source.id,
        limits=IngestLimits(max_extractions=0, max_comparisons=5),
        storage_root=tmp_path,
        write_report_file=False,
    )
    assert r2.comparisons_unchanged == 1 or r2.comparisons_changed == 1
    assert (
        db_session.scalar(select(func.count()).select_from(DocumentContentComparison))
        == 1
    )


def test_extraction_backlog_drains_across_bounded_runs(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    for i in range(3):
        _add_doc_version(
            db_session,
            tmp_path,
            source,
            url=f"{source.seed_url_normalized}/page-{i}",
            version_number=1,
            html=SAMPLE_HTML,
            raw_sha=f"{i:02x}" * 32,
            is_current=True,
        )

    first = run_ingestion(
        db_session,
        execute=True,
        mode=MODE_PROCESS_EXISTING,
        source_id=source.id,
        limits=IngestLimits(max_extractions=1, max_comparisons=0),
        storage_root=tmp_path,
        write_report_file=False,
    )
    assert first.extractions_success == 1
    assert first.extraction_backlog_remaining == 2
    assert "max_extractions" in first.limits_reached

    second = run_ingestion(
        db_session,
        execute=True,
        mode=MODE_PROCESS_EXISTING,
        source_id=source.id,
        limits=IngestLimits(max_extractions=1, max_comparisons=0),
        storage_root=tmp_path,
        write_report_file=False,
    )
    assert second.extractions_success == 1
    assert second.extraction_backlog_remaining == 1

    third = run_ingestion(
        db_session,
        execute=True,
        mode=MODE_PROCESS_EXISTING,
        source_id=source.id,
        limits=IngestLimits(max_extractions=5, max_comparisons=0),
        storage_root=tmp_path,
        write_report_file=False,
    )
    assert third.extractions_success == 1
    assert third.extraction_backlog_remaining == 0
    assert db_session.scalar(select(func.count()).select_from(DocumentExtraction)) == 3


def test_partial_failure_continues_and_rerun_recovers(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session)
    good, good_v = _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=f"{source.seed_url_normalized}/good",
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="66" * 32,
        is_current=True,
    )
    bad, bad_v = _add_doc_version(
        db_session,
        tmp_path,
        source,
        url=f"{source.seed_url_normalized}/bad",
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="77" * 32,
        is_current=True,
    )
    # Corrupt storage path so extraction fails for bad doc only.
    bad_v.storage_path = "raw/documents/missing/v1.html"
    db_session.flush()

    report = run_ingestion(
        db_session,
        execute=True,
        mode=MODE_PROCESS_EXISTING,
        source_id=source.id,
        limits=IngestLimits(max_extractions=10, max_comparisons=0),
        storage_root=tmp_path,
        write_report_file=False,
    )
    assert report.extractions_success >= 1
    assert report.extractions_failed >= 1
    assert report.partial is True
    assert db_session.get(Document, good.id) is not None

    # Restore path and rerun — backlog drains for the previously failed version.
    bad_v.storage_path = f"raw/documents/{bad.id}/v1.html"
    write_bytes_atomic(tmp_path / bad_v.storage_path, SAMPLE_HTML)
    db_session.flush()
    rerun = run_ingestion(
        db_session,
        execute=True,
        mode=MODE_PROCESS_EXISTING,
        source_id=source.id,
        limits=IngestLimits(max_extractions=10, max_comparisons=0),
        storage_root=tmp_path,
        write_report_file=False,
    )
    assert rerun.extractions_success >= 1
    assert rerun.extraction_backlog_remaining == 0


def test_per_source_report_accuracy(db_session, tmp_path: Path):
    _clear(db_session)
    s1 = _seed_source(db_session, name="rep-a")
    s2 = _seed_source(db_session, name="rep-b")
    _add_doc_version(
        db_session,
        tmp_path,
        s1,
        url=s1.seed_url_normalized,
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="88" * 32,
        is_current=True,
    )
    _add_doc_version(
        db_session,
        tmp_path,
        s2,
        url=s2.seed_url_normalized,
        version_number=1,
        html=SAMPLE_HTML,
        raw_sha="99" * 32,
        is_current=True,
    )
    report = run_ingestion(
        db_session,
        execute=True,
        mode=MODE_PROCESS_EXISTING,
        all_approved=True,
        limits=IngestLimits(max_extractions=10, max_comparisons=0),
        storage_root=tmp_path,
        write_report_file=False,
    )
    assert report.extractions_success == 2
    by_id = {s.source_id: s for s in report.per_source}
    assert by_id[s1.id].extractions_success == 1
    assert by_id[s2.id].extractions_success == 1
    assert report.mode == MODE_PROCESS_EXISTING
