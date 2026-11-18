"""Phase 10B.2 tests: source date extraction and temporal metadata propagation.

All database tests use the isolated ``*_test`` database via ``db_session``.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pymupdf
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lifepilot.agent.answer import build_user_prompt, pack_evidence_context
from lifepilot.agent.evidence import (
    EvidenceSourceType,
    EvidenceVerificationState,
    evidence_from_external_item,
)
from lifepilot.agent.orchestration import EvidenceOrchestrator
from lifepilot.crawl.fetcher import (
    STATUS_FETCHED_NEW_VERSION,
    STATUS_UNCHANGED,
    fetch_approved_seed,
    fetch_binary_document_url,
)
from lifepilot.crawl.policy import fetcher_policy
from lifepilot.db.isolation import clear_registry_tables
from lifepilot.db.models import (
    PLANNED_EMBEDDING_MODEL_ID,
    Authority,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.db.temporal import (
    extract_http_header_temporal,
    parse_http_datetime,
    parse_pdf_datetime,
    parse_source_datetime,
)
from lifepilot.external.cache import ResponseCache
from lifepilot.external.http import ExternalHttpClient, HttpClientConfig
from lifepilot.external.models import ExternalQuery, ProviderId
from lifepilot.external.router import ExternalKnowledgeRouter
from lifepilot.extract.html_structured import (
    extract_html_document,
    extract_html_temporal_metadata,
    normalized_content_sha256,
)
from lifepilot.extract.pdf_structured import extract_pdf_document
from lifepilot.extract.runner import STATUS_SUCCESS, STATUS_UNCHANGED as EXT_UNCHANGED
from lifepilot.extract.runner import extract_document_version
from lifepilot.rag.embedder import FakeEmbedder
from lifepilot.registry.importer import import_seed_sources
from lifepilot.registry.review import set_source_review_status


def _sha(val: str) -> str:
    return hashlib.sha256(val.encode("utf-8")).hexdigest()


def _stream_response(
    *,
    status_code: int = 200,
    headers: list[tuple[str, str]] | dict[str, str] | None = None,
    body: bytes = b"<html><body><main><h1>Test</h1><p>Content here.</p></main></body></html>",
):
    stream = MagicMock()
    stream.status_code = status_code
    hdr_obj = httpx.Headers(headers or [("content-type", "text/html; charset=utf-8")])
    stream.headers = hdr_obj
    stream.iter_bytes.side_effect = lambda: iter([body])
    stream.close = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = stream
    cm.__exit__.return_value = False
    return cm


def _mock_http_client(
    *,
    headers: list[tuple[str, str]] | dict[str, str] | None = None,
    body: bytes = b"<html><body><main><h1>Test</h1><p>Substantive body.</p></main></body></html>",
    stream_side_effect=None,
) -> MagicMock:
    client = MagicMock()
    client.get.return_value = MagicMock(status_code=200, text="User-agent: *\nAllow: /\n")
    if stream_side_effect is not None:
        client.stream.side_effect = stream_side_effect
    else:
        client.stream.return_value = _stream_response(headers=headers, body=body)
    return client


def _approve_gb_source(session: Session) -> Source:
    clear_registry_tables(session)
    import_seed_sources(session, verify=False)
    session.flush()
    source = session.scalar(
        select(Source).where(
            Source.seed_url_normalized == "https://www.gov.uk/skilled-worker-visa"
        )
    )
    assert source is not None
    set_source_review_status(session, source_id=source.id, status="approved")
    session.flush()
    return source


# ---------------------------------------------------------------------------
# HTTP Last-Modified, ETag, and timezone parsing
# ---------------------------------------------------------------------------


def test_http_last_modified_valid_malformed_and_conflicting():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)

    # Valid RFC 7231 + strong ETag
    ok = extract_http_header_temporal(
        [
            ("Content-Type", "text/html"),
            ("Last-Modified", "Wed, 21 Oct 2026 07:28:00 GMT"),
            ("ETag", '  "v42-abc"  '),
        ],
        retrieved_at=now,
    )
    assert ok.http_last_modified == datetime(2026, 10, 21, 7, 28, tzinfo=timezone.utc)
    assert ok.http_etag == '"v42-abc"'
    assert ok.warnings == []

    # Weak ETag preserved, missing Last-Modified stays None
    weak = extract_http_header_temporal([("ETag", 'W/"weak-99"')], retrieved_at=now)
    assert weak.http_last_modified is None
    assert weak.http_etag == 'W/"weak-99"'

    # Malformed Last-Modified does not crash and does not invent date
    bad = extract_http_header_temporal(
        [("Last-Modified", "not-a-valid-date"), ("ETag", "   ")],
        retrieved_at=now,
    )
    assert bad.http_last_modified is None
    assert bad.http_etag is None
    assert any("malformed_http_last_modified" in w for w in bad.warnings)

    # Conflicting Last-Modified headers are not silently resolved
    conflict = extract_http_header_temporal(
        [
            ("Last-Modified", "Mon, 01 Jun 2026 00:00:00 GMT"),
            ("Last-Modified", "Tue, 02 Jun 2026 00:00:00 GMT"),
        ],
        retrieved_at=now,
    )
    assert conflict.http_last_modified is None
    assert any("conflicting_http_last_modified_headers" in w for w in conflict.warnings)
    assert len(conflict.observations) == 2

    # Future clock skew flagged while retaining parsed timestamp
    skew = extract_http_header_temporal(
        [("Last-Modified", "Fri, 25 Dec 2026 00:00:00 GMT")],
        retrieved_at=now,
    )
    assert skew.http_last_modified == datetime(2026, 12, 25, 0, 0, tzinfo=timezone.utc)
    assert "http_last_modified_future_clock_skew" in skew.warnings


def test_timezone_normalization_and_ambiguous_dates():
    dt_offset, err, status = parse_source_datetime("2026-08-27T14:30:00+02:00")
    assert err is None
    assert status == "ok"
    assert dt_offset == datetime(2026, 8, 27, 12, 30, tzinfo=timezone.utc)

    dt_named, err2, status2 = parse_source_datetime("27 August 2026")
    assert err2 is None
    assert status2 == "ok_date_only"
    assert dt_named == datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)

    # Ambiguous DD/MM vs MM/DD must not be guessed.
    dt_ambig, err3, status3 = parse_source_datetime("04/05/2026")
    assert dt_ambig is None
    assert status3 == "ambiguous"
    assert err3 == "ambiguous_numeric_date"

    dt_http, err4 = parse_http_datetime("Thu, 27 Aug 2026 10:00:00 +0200")
    assert err4 is None
    assert dt_http == datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# HTML publication/update dates, JSON-LD, banners, conflicts, and hash compat
# ---------------------------------------------------------------------------


def test_html_meta_jsonld_and_real_fixture_date_extraction():
    html = b"""<!DOCTYPE html>
    <html>
    <head>
      <title>Skilled Worker visa - GOV.UK</title>
      <meta name="govuk:first-published-at" content="2020-12-01T09:00:00Z" />
      <meta property="article:modified_time" content="2026-07-22T11:30:00+01:00" />
      <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@graph": [{
          "@type": "WebPage",
          "datePublished": "2020-12-01T09:00:00Z",
          "dateModified": "2026-07-22T10:30:00Z"
        }]
      }
      </script>
    </head>
    <body>
      <main id="content">
        <h1>Skilled Worker visa</h1>
        <p>Applications made before 4 April 2030 may qualify under transitional rules.</p>
      </main>
    </body>
    </html>"""
    doc = extract_html_document(html)
    tm = doc.temporal_metadata
    assert tm["source_published_at"] == "2020-12-01T09:00:00+00:00"
    assert tm["source_updated_at"] == "2026-07-22T10:30:00+00:00"
    assert tm["has_conflicts"] is False
    assert tm["published_provenance"]["extraction_method"] == "html_json_ld"
    assert tm["updated_provenance"]["extraction_method"] == "html_json_ld"
    # Body prose date (4 April 2030) must never be misread as publication date.
    assert not any("2030" in (o.get("normalized_utc") or "") for o in tm["observations"])

    # Real Singapore MOM fixture includes hidden mom-last-updated banner.
    from pathlib import Path

    mom_bytes = (
        Path(__file__).resolve().parent / "fixtures" / "html" / "mom_employment_pass.html"
    ).read_bytes()
    mom_doc = extract_html_document(mom_bytes)
    assert mom_doc.temporal_metadata["source_updated_at"] == "2026-08-27T00:00:00+00:00"
    assert (
        mom_doc.temporal_metadata["updated_provenance"]["extraction_method"]
        == "html_gov_banner"
    )


def test_html_conflicting_dates_use_precedence_and_record_conflict():
    html = b"""<!DOCTYPE html>
    <html>
    <head>
      <title>Conflicting dates</title>
      <meta property="article:published_time" content="2025-02-01T00:00:00Z" />
      <meta property="article:modified_time" content="2025-03-01T00:00:00Z" />
      <script type="application/ld+json">
      {"datePublished": "2026-01-10T00:00:00Z", "dateModified": "2026-01-15T00:00:00Z"}
      </script>
    </head>
    <body>
      <main><h1>Guide</h1><p>Guidance body text.</p></main>
      <div class="last-updated">Last Updated: 14 January 2026</div>
    </body>
    </html>"""
    tm = extract_html_temporal_metadata(html)
    assert tm["has_conflicts"] is True
    # JSON-LD wins by deterministic precedence.
    assert tm["source_published_at"] == "2026-01-10T00:00:00+00:00"
    assert tm["source_updated_at"] == "2026-01-15T00:00:00+00:00"
    assert tm["published_provenance"]["has_conflict"] is True
    assert tm["updated_provenance"]["has_conflict"] is True
    assert len(tm["conflicts"]) >= 2


def test_html_missing_dates_and_content_hash_compatibility():
    html_no_dates = b"<html><head><title>Plain</title></head><body><main><h1>Plain</h1><p>Body text.</p></main></body></html>"
    html_with_dates = b"""<html><head><title>Plain</title>
    <meta property="article:published_time" content="2026-01-01T00:00:00Z" />
    </head><body><main><h1>Plain</h1><p>Body text.</p></main></body></html>"""

    doc_plain = extract_html_document(html_no_dates)
    doc_dated = extract_html_document(html_with_dates)
    assert doc_plain.temporal_metadata == {}
    assert doc_dated.temporal_metadata["source_published_at"] == "2026-01-01T00:00:00+00:00"
    # Canonical content hash stays identical when only temporal metadata differs.
    assert normalized_content_sha256(doc_plain) == normalized_content_sha256(doc_dated)


# ---------------------------------------------------------------------------
# PDF metadata parsing & distinction from official publication dates
# ---------------------------------------------------------------------------


def test_pdf_metadata_timezone_offsets_and_persisted_distinction(db_session: Session, tmp_path):
    dt_plus, err_p, st_p = parse_pdf_datetime("D:20260401153000+02'00'")
    assert err_p is None and st_p == "ok"
    assert dt_plus == datetime(2026, 4, 1, 13, 30, tzinfo=timezone.utc)

    dt_minus, err_m, st_m = parse_pdf_datetime("D:20260401153000-05'00'")
    assert err_m is None and st_m == "ok"
    assert dt_minus == datetime(2026, 4, 1, 20, 30, tzinfo=timezone.utc)

    dt_bad, err_b, _ = parse_pdf_datetime("D:20261345999999")
    assert dt_bad is None and err_b is not None

    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Employment permit checklist", fontsize=16)
    page.insert_text((72, 110), "Submit signed employer declaration.", fontsize=11)
    pdf.set_metadata(
        {
            "title": "Checklist",
            "creationDate": "D:20260301100000+02'00'",
            "modDate": "D:20260415120000Z",
        }
    )
    payload = pdf.tobytes()
    pdf.close()

    extracted = extract_pdf_document(payload)
    tm = extracted.temporal_metadata
    assert tm["file_created_at"] == "2026-03-01T08:00:00+00:00"
    assert tm["file_modified_at"] == "2026-04-15T12:00:00+00:00"
    assert tm["source_published_at"] is None
    assert tm["source_updated_at"] is None
    assert all(o["is_file_metadata"] is True for o in tm["observations"])

    # Persist into DB and verify file timestamps stay in temporal_notes, NOT source_published_at.
    source = _approve_gb_source(db_session)
    doc = Document(
        authority_id=source.authority_id,
        primary_source_id=source.id,
        canonical_url="https://www.gov.uk/skilled-worker-visa/checklist.pdf",
        url_normalized="https://www.gov.uk/skilled-worker-visa/checklist.pdf",
        content_kind="pdf",
        language="en",
        discovery_status="discovered",
    )
    db_session.add(doc)
    db_session.flush()
    rel = f"raw/documents/{doc.id}/v1.pdf"
    (tmp_path / "raw" / "documents" / str(doc.id)).mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_bytes(payload)
    ver = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=source.id,
        version_number=1,
        source_url=doc.canonical_url,
        content_type="application/pdf",
        retrieved_at=datetime.now(timezone.utc),
        http_status=200,
        content_sha256=_sha("pdf"),
        byte_size=len(payload),
        storage_path=rel,
        is_current=True,
    )
    db_session.add(ver)
    db_session.flush()

    res = extract_document_version(db_session, ver, doc, persist=True, storage_root=tmp_path)
    assert res.status == STATUS_SUCCESS
    db_session.refresh(ver)
    assert ver.source_published_at is None
    assert ver.source_updated_at is None
    assert ver.legal_status == "unknown"
    notes = json.loads(ver.temporal_notes or "{}")
    assert notes["pdf_file_created_at"] == "2026-03-01T08:00:00+00:00"
    assert notes["pdf_file_modified_at"] == "2026-04-15T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Ingestion persistence, idempotency, and safe non-overwrite rules
# ---------------------------------------------------------------------------


def test_http_and_extract_ingestion_idempotency_and_safe_persistence(
    db_session: Session, tmp_path
):
    source = _approve_gb_source(db_session)
    html = b"""<!DOCTYPE html><html><head><title>Skilled Worker</title>
    <meta property="article:published_time" content="2025-04-04T00:00:00Z" />
    <meta property="article:modified_time" content="2026-07-22T00:00:00Z" />
    </head><body><main id="content"><h1>Skilled Worker</h1>
    <p>Applicants need a licensed sponsor and Certificate of Sponsorship.</p>
    </main></body></html>"""

    # Fetch 1: valid Last-Modified + ETag -> persisted on DocumentVersion with legal_status='unknown'
    c1 = _mock_http_client(
        headers=[
            ("content-type", "text/html; charset=utf-8"),
            ("last-modified", "Wed, 22 Jul 2026 09:15:00 GMT"),
            ("etag", '"etag-1"'),
        ],
        body=html,
    )
    r1 = fetch_approved_seed(
        db_session, source.id, persist=True, policy=fetcher_policy(), http_client=c1, storage_root=tmp_path
    )
    assert r1.status == STATUS_FETCHED_NEW_VERSION
    ver = db_session.get(DocumentVersion, r1.version_id)
    assert ver is not None
    orig_retrieved_at = ver.retrieved_at
    assert ver.http_last_modified == datetime(2026, 7, 22, 9, 15, tzinfo=timezone.utc)
    assert ver.http_etag == '"etag-1"'
    assert ver.legal_status == "unknown"

    # Extract -> persists source_published_at / source_updated_at without setting in_force
    doc = db_session.get(Document, r1.document_id)
    assert doc is not None
    ex1 = extract_document_version(db_session, ver, doc, persist=True, storage_root=tmp_path)
    assert ex1.status == STATUS_SUCCESS
    db_session.refresh(ver)
    assert ver.source_published_at == datetime(2025, 4, 4, 0, 0, tzinfo=timezone.utc)
    assert ver.source_updated_at == datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
    assert ver.legal_status == "unknown"

    # Fetch 2: unchanged content, missing headers -> no new version & valid headers NOT erased
    c2 = _mock_http_client(
        headers=[("content-type", "text/html; charset=utf-8")],
        body=html,
    )
    r2 = fetch_approved_seed(
        db_session, source.id, persist=True, policy=fetcher_policy(), http_client=c2, storage_root=tmp_path
    )
    assert r2.status == STATUS_UNCHANGED
    assert db_session.scalar(select(func.count()).select_from(DocumentVersion)) == 1
    db_session.refresh(ver)
    assert ver.http_last_modified == datetime(2026, 7, 22, 9, 15, tzinfo=timezone.utc)
    assert ver.http_etag == '"etag-1"'
    assert ver.retrieved_at == orig_retrieved_at

    # Fetch 3: unchanged content, changed headers -> no new version, prior valid kept, trace recorded
    c3 = _mock_http_client(
        headers=[
            ("content-type", "text/html; charset=utf-8"),
            ("last-modified", "Thu, 01 Oct 2026 10:00:00 GMT"),
            ("etag", '"etag-2"'),
        ],
        body=html,
    )
    r3 = fetch_approved_seed(
        db_session, source.id, persist=True, policy=fetcher_policy(), http_client=c3, storage_root=tmp_path
    )
    assert r3.status == STATUS_UNCHANGED
    assert db_session.scalar(select(func.count()).select_from(DocumentVersion)) == 1
    db_session.refresh(ver)
    assert ver.http_etag == '"etag-1"'
    notes = json.loads(ver.temporal_notes or "{}")
    assert any(
        ev.get("event") == "http_etag_changed_on_unchanged_body"
        for ev in (notes.get("history") or [])
    )

    # Repeat extraction -> idempotent STATUS_UNCHANGED
    ex2 = extract_document_version(db_session, ver, doc, persist=True, storage_root=tmp_path)
    assert ex2.status == EXT_UNCHANGED


def test_binary_fetch_captures_http_headers(db_session: Session, tmp_path):
    source = _approve_gb_source(db_session)
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "Visa PDF body")
    pdf_bytes = pdf.tobytes()
    pdf.close()

    client = _mock_http_client(
        headers=[
            ("content-type", "application/pdf"),
            ("last-modified", "Tue, 10 Nov 2026 08:00:00 GMT"),
            ("etag", 'W/"pdf-1"'),
        ],
        body=pdf_bytes,
    )
    res = fetch_binary_document_url(
        db_session,
        source,
        url="https://www.gov.uk/skilled-worker-visa/guide.pdf",
        expected_kind="pdf",
        persist=True,
        policy=fetcher_policy(),
        http_client=client,
        storage_root=tmp_path,
    )
    assert res.status == STATUS_FETCHED_NEW_VERSION
    ver = db_session.get(DocumentVersion, res.version_id)
    assert ver is not None
    assert ver.http_last_modified == datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
    assert ver.http_etag == 'W/"pdf-1"'


# ---------------------------------------------------------------------------
# External connectors, EvidenceItem propagation, legacy RAG & packing
# ---------------------------------------------------------------------------


def test_external_connector_dates_and_legal_status_propagate_to_evidence_item():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "gov.uk/api/content" in url:
            return httpx.Response(
                200,
                json={
                    "base_path": "/skilled-worker-visa",
                    "content_id": "cid-1",
                    "title": "Skilled Worker visa",
                    "first_published_at": "2020-12-01T09:00:00Z",
                    "public_updated_at": "2026-07-22T10:30:00Z",
                    "withdrawn_notice": {"explanation": "Replaced by new route"},
                    "details": {
                        "body": "<p>You must work for a licensed UK sponsor under the Skilled Worker rules.</p>"
                    },
                },
                request=request,
            )
        if "federalregister.gov" in url:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "document_number": "2026-12345",
                            "title": "H-1B Proposed Modernization",
                            "type": "Proposed Rule",
                            "abstract": "Proposes updated specialty occupation registration procedures.",
                            "html_url": "https://www.federalregister.gov/d/2026-12345",
                            "publication_date": "2026-10-01",
                            "effective_on": "2027-01-15",
                            "agencies": [{"name": "DHS"}],
                        }
                    ]
                },
                request=request,
            )
        if "ind.nl" in url:
            return httpx.Response(
                200,
                text="""<!DOCTYPE html><html><head>
                <meta property="article:modified_time" content="2026-09-01T00:00:00Z" />
                </head><body><main><h1>Highly skilled migrant</h1>
                <p>Employers must be recognised sponsors by IND and meet salary criteria.</p>
                </main></body></html>""",
                headers={"Last-Modified": "Tue, 01 Sep 2026 06:00:00 GMT"},
                request=request,
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    raw = httpx.Client(transport=transport, follow_redirects=True)
    http = ExternalHttpClient(
        config=HttpClientConfig(timeout_seconds=5.0, max_retries=1),
        cache=ResponseCache(default_ttl_seconds=60.0, disk_dir=None),
        client=raw,
        allowed_hosts=None,
    )
    router = ExternalKnowledgeRouter(http=http, enable_disk_cache=False)

    @contextmanager
    def _dummy_sf():
        yield MagicMock()

    orch = EvidenceOrchestrator(
        session_factory=_dummy_sf,
        external_router=router,
    )
    orch._retrieve_internal = lambda **_kw: ([], "no_results", 0.5)  # type: ignore[method-assign]

    gb_router_out = router.query(
        ExternalQuery(query="Skilled Worker", jurisdiction="GB", pathway_id="gb_skilled_worker")
    )
    assert gb_router_out.items
    raw_gb_ev = evidence_from_external_item(
        jurisdiction="GB",
        official_url=gb_router_out.items[0].official_url,
        provider=gb_router_out.items[0].provider.value,
        passage_text=gb_router_out.items[0].passages[0],
        published_at=gb_router_out.items[0].published_at,
        updated_at=gb_router_out.items[0].updated_at,
        legal_status=gb_router_out.items[0].legal_status,
    )
    assert raw_gb_ev.source_published_at == "2020-12-01T09:00:00Z"
    assert raw_gb_ev.source_updated_at == "2026-07-22T10:30:00Z"
    assert raw_gb_ev.legal_status == "withdrawn"
    assert raw_gb_ev.explicit_effective_at is None

    # In current-law orchestration (10B.3), withdrawn/proposed external items are excluded.
    gb_res = orch.gather(
        question="Skilled Worker", jurisdiction="GB", external_mode="on"
    )
    assert gb_res.evidence == []
    assert any(
        e.get("reason") == "excluded_status_for_current:withdrawn"
        for e in gb_res.excluded_evidence
    )

    us_router_out = router.query(
        ExternalQuery(query="H-1B registration", jurisdiction="US")
    )
    raw_us_ev = evidence_from_external_item(
        jurisdiction="US",
        official_url=us_router_out.items[0].official_url,
        provider=us_router_out.items[0].provider.value,
        passage_text=us_router_out.items[0].passages[0],
        published_at=us_router_out.items[0].published_at,
        effective_at=us_router_out.items[0].effective_at,
        legal_status=us_router_out.items[0].legal_status,
    )
    assert raw_us_ev.source_published_at == "2026-10-01"
    assert raw_us_ev.explicit_effective_at == "2027-01-15"
    assert raw_us_ev.legal_status == "proposed_not_effective"
    assert raw_us_ev.temporal_provenance["effective_at_is_connector_assertion"] is True
    assert raw_us_ev.temporal_provenance["legal_status_is_connector_assertion"] is True

    us_res = orch.gather(
        question="H-1B registration", jurisdiction="US", external_mode="on"
    )
    assert us_res.evidence == []
    assert any(
        "proposed_not_effective" in (e.get("reason") or "")
        for e in us_res.excluded_evidence
    )

    nl_res = orch.gather(
        question="Highly skilled migrant",
        jurisdiction="NL",
        pathway_id="nl_highly_skilled_migrant",
        external_mode="on",
    )
    assert nl_res.evidence
    nl_ev = nl_res.evidence[0]
    assert nl_ev.source_updated_at == "2026-09-01T00:00:00+00:00"
    assert nl_ev.http_last_modified == "2026-09-01T06:00:00+00:00"
    assert nl_ev.explicit_effective_at is None
    assert nl_ev.legal_status == "unknown"


def test_legacy_internal_document_and_evidence_packing_with_known_and_unknown_dates(
    db_session: Session,
):
    """Legacy DB rows with NULL temporal metadata and rich rows both pack safely."""
    jur = Jurisdiction(
        code="GB", name="United Kingdom", jurisdiction_type="country", country_code="GB"
    )
    db_session.add(jur)
    db_session.flush()
    auth = Authority(jurisdiction_id=jur.id, name="Home Office")
    db_session.add(auth)
    db_session.flush()
    src = Source(
        authority_id=auth.id,
        jurisdiction_id=jur.id,
        name="GOV.UK",
        seed_url="https://www.gov.uk/skilled-worker-visa",
        seed_url_normalized="https://www.gov.uk/skilled-worker-visa",
        review_status="approved",
    )
    db_session.add(src)
    db_session.flush()
    doc = Document(
        authority_id=auth.id,
        primary_source_id=src.id,
        canonical_url="https://www.gov.uk/skilled-worker-visa/legacy",
        url_normalized="https://www.gov.uk/skilled-worker-visa/legacy",
        content_kind="html_page",
        language="en",
        title="Skilled Worker visa overview",
        discovery_status="seed",
    )
    db_session.add(doc)
    db_session.flush()
    ver = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=src.id,
        version_number=1,
        source_url=doc.canonical_url,
        content_type="text/html",
        retrieved_at=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
        http_status=200,
        content_sha256=_sha("legacy"),
        byte_size=80,
        storage_path="raw/documents/legacy/v1.html",
        is_current=True,
    )
    db_session.add(ver)
    db_session.flush()

    text_val = (
        "Skilled Worker visa applicants must work for an approved UK sponsor "
        "with a valid Certificate of Sponsorship."
    )
    chunk = DocumentChunk(
        document_id=doc.id,
        document_version_id=ver.id,
        source_id=src.id,
        jurisdiction_id=jur.id,
        jurisdiction_code="GB",
        chunk_index=0,
        heading_path="Overview > Sponsorship",
        content_text=text_val,
        content_sha256=_sha(text_val),
        is_current_version=True,
    )
    db_session.add(chunk)
    db_session.flush()
    fake_emb = FakeEmbedder()
    vec = fake_emb.embed_passages([text_val]).vectors[0]
    db_session.add(
        DocumentChunkEmbedding(
            chunk_id=chunk.id,
            embedding_model_id=PLANNED_EMBEDDING_MODEL_ID,
            embedding=list(map(float, vec.tolist())),
        )
    )
    db_session.flush()

    @contextmanager
    def _bound_cm():
        yield db_session

    orch = EvidenceOrchestrator(session_factory=_bound_cm, embedder=fake_emb)
    res = orch.gather(
        question="Skilled Worker sponsor",
        jurisdiction="GB",
        external_mode="off",
    )
    assert res.evidence
    internal_item = res.evidence[0]
    assert internal_item.title == "Skilled Worker visa overview"
    assert internal_item.heading_path == "Overview > Sponsorship"
    assert internal_item.source_published_at is None
    assert internal_item.legal_status == "unknown"
    assert internal_item.explicit_effective_at is None
    assert internal_item.retrieved_at is not None
    assert internal_item.verification_state == EvidenceVerificationState.UNVERIFIED_RETRIEVAL

    # Combine with an external item carrying explicit effective_at & proposed status.
    ext_item = evidence_from_external_item(
        jurisdiction="GB",
        official_url="https://www.gov.uk/skilled-worker-visa/future",
        provider=ProviderId.GOVUK_CONTENT.value,
        passage_text="Proposed rule text: future changes do not apply immediately.",
        title="Proposed Skilled Worker Update",
        published_at="2026-10-15T00:00:00Z",
        updated_at="2026-11-01T00:00:00Z",
        effective_at="2027-04-01",
        legal_status="future_pending",
    )

    packed = pack_evidence_context(
        [internal_item, ext_item],
        max_items=5,
        max_chars_per_passage=80,
        max_total_chars=500,
    )
    assert len(packed) == 2
    assert packed[0]["explicit_effective_at"] == "unknown"
    assert packed[0]["legal_status"] == "unknown"
    assert packed[0]["heading_path"] == "Overview > Sponsorship"
    assert packed[1]["explicit_effective_at"] == "2027-04-01"
    assert packed[1]["legal_status"] == "future_pending"
    assert "connector-provided effective_at is unverified" in (
        packed[1]["temporal_note"] or ""
    )

    prompt = build_user_prompt(
        question="Skilled Worker sponsor",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        user_circumstances=None,
        evidence_payload=packed,
    )
    assert "explicit_effective_at: unknown" in prompt
    assert "explicit_effective_at: 2027-04-01" in prompt
    assert "legal_status: future_pending" in prompt
    assert "do not establish current legal applicability" in prompt
