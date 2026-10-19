"""Mocked tests for bounded HTML link discovery."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy import delete, func, select

from lifepilot.crawl.discover import discover_from_document, discover_from_source_seed
from lifepilot.crawl.fetcher import (
    STATUS_DRY_RUN,
    STATUS_FETCHED_NEW_VERSION,
    fetch_approved_seed,
    fetch_html_document_url,
)
from lifepilot.crawl.links import discover_links_from_html, resolve_and_classify_link
from lifepilot.crawl.policy import fetcher_policy
from lifepilot.db.models import Authority, Document, DocumentVersion, Jurisdiction, Source
from lifepilot.registry.importer import import_seed_sources
from lifepilot.registry.review import set_source_review_status
from lifepilot.registry.urls import normalize_url, path_prefix_pattern


def _clear_registry(session) -> None:
    session.execute(delete(DocumentVersion))
    session.execute(delete(Document))
    session.execute(delete(Source))
    session.execute(delete(Authority))
    session.execute(delete(Jurisdiction))
    session.flush()


def _approve_one(session) -> Source:
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


def _stream_response(
    *,
    status_code: int = 200,
    headers: dict | None = None,
    body: bytes = b"<html><body>ok</body></html>",
):
    stream = MagicMock()
    stream.status_code = status_code
    stream.headers = headers or {"content-type": "text/html; charset=utf-8"}
    stream.iter_bytes.side_effect = lambda: iter([body])
    stream.close = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = stream
    cm.__exit__.return_value = False
    return cm


def _mock_client(
    *,
    html: bytes = b"<html><body>hello</body></html>",
    content_type: str = "text/html; charset=utf-8",
    robots_text: str = "User-agent: *\nAllow: /\n",
) -> MagicMock:
    client = MagicMock()
    client.get.return_value = MagicMock(status_code=200, text=robots_text)
    client.stream.return_value = _stream_response(
        headers={"content-type": content_type},
        body=html,
    )
    return client


def test_classify_in_scope_html_and_pdf():
    patterns = ["https://www.gov.uk/skilled-worker-visa*"]
    base = "https://www.gov.uk/skilled-worker-visa"
    html_link = resolve_and_classify_link(
        "/skilled-worker-visa/eligibility",
        base_url=base,
        allowed_patterns=patterns,
    )
    pdf_link = resolve_and_classify_link(
        "/skilled-worker-visa/guidance.pdf",
        base_url=base,
        allowed_patterns=patterns,
    )
    out = resolve_and_classify_link(
        "https://www.gov.uk/browse/visas-immigration",
        base_url=base,
        allowed_patterns=patterns,
    )
    assert html_link.kind == "in_scope_html"
    assert pdf_link.kind == "in_scope_pdf"
    assert out.kind == "out_of_scope"


def test_discover_links_dedupes():
    html = """
    <html><body>
      <a href="/skilled-worker-visa/eligibility">A</a>
      <a href="/skilled-worker-visa/eligibility#section">B</a>
      <a href="mailto:x@example.com">mail</a>
    </body></html>
    """
    links = discover_links_from_html(
        html,
        base_url="https://www.gov.uk/skilled-worker-visa",
        allowed_patterns=["https://www.gov.uk/skilled-worker-visa*"],
    )
    html_links = [link for link in links if link.kind == "in_scope_html"]
    assert len(html_links) == 1
    assert html_links[0].url_normalized == (
        "https://www.gov.uk/skilled-worker-visa/eligibility"
    )


def test_discover_dry_run_lists_without_fetch(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_one(db_session)
    seed_html = b"""
    <html><body>
      <a href="/skilled-worker-visa/eligibility">Eligibility</a>
      <a href="/skilled-worker-visa/docs.pdf">PDF</a>
      <a href="https://www.gov.uk/browse/visas-immigration">Out</a>
    </body></html>
    """
    client = _mock_client(html=seed_html)
    seed = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=client,
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    assert seed.status == STATUS_FETCHED_NEW_VERSION
    assert seed.document_id is not None

    client.stream.reset_mock()
    report = discover_from_document(
        db_session,
        seed.document_id,
        persist=False,
        policy=fetcher_policy(max_discovered_html_fetches_per_run=10),
        http_client=client,
        storage_root=tmp_path,
    )
    db_session.flush()

    assert len(report.in_scope_html) == 1
    assert report.in_scope_html[0].url_normalized.endswith("/eligibility")
    assert len(report.in_scope_pdf) == 1
    assert len(report.out_of_scope) == 1
    assert report.fetch_results
    assert all(r.status == STATUS_DRY_RUN for r in report.fetch_results)
    client.stream.assert_not_called()
    # Only the seed document exists.
    assert db_session.scalar(select(func.count()).select_from(Document)) == 1


def test_discover_persist_fetches_in_scope_html_only(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_one(db_session)
    seed_html = b"""
    <html><body>
      <a href="/skilled-worker-visa/eligibility">Eligibility</a>
      <a href="/skilled-worker-visa/docs.pdf">PDF</a>
    </body></html>
    """
    seed_client = _mock_client(html=seed_html)
    seed = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=seed_client,
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()

    child_html = b"<html><body>eligibility page</body></html>"
    discover_client = _mock_client(html=child_html)
    report = discover_from_source_seed(
        db_session,
        source.id,
        persist=True,
        policy=fetcher_policy(max_discovered_html_fetches_per_run=10),
        http_client=discover_client,
        storage_root=tmp_path,
    )
    db_session.flush()

    assert len(report.in_scope_pdf) == 1
    assert any(r.status == STATUS_FETCHED_NEW_VERSION for r in report.fetch_results)
    docs = list(db_session.scalars(select(Document)).all())
    assert len(docs) == 2
    discovered = [d for d in docs if d.discovery_status == "discovered"]
    assert len(discovered) == 1
    assert discovered[0].url_normalized.endswith("/eligibility")
    version = db_session.scalar(
        select(DocumentVersion).where(DocumentVersion.document_id == discovered[0].id)
    )
    assert version is not None
    assert (tmp_path / version.storage_path).read_bytes() == child_html


def test_discover_respects_html_fetch_cap(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_one(db_session)
    seed_html = b"""
    <html><body>
      <a href="/skilled-worker-visa/a">A</a>
      <a href="/skilled-worker-visa/b">B</a>
      <a href="/skilled-worker-visa/c">C</a>
    </body></html>
    """
    fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=_mock_client(html=seed_html),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()

    report = discover_from_source_seed(
        db_session,
        source.id,
        persist=True,
        policy=fetcher_policy(max_discovered_html_fetches_per_run=2),
        http_client=_mock_client(html=b"<html>child</html>"),
        storage_root=tmp_path,
    )
    db_session.flush()

    assert len(report.in_scope_html) == 3
    assert len(report.fetch_results) == 2
    discovered_count = db_session.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.discovery_status == "discovered")
    )
    assert discovered_count == 2


def _overlapping_sources(session) -> tuple[Source, Source]:
    """Broad work* source and nested work/hsm* source (IND-like overlap)."""
    jurisdiction = Jurisdiction(
        code="XX",
        name="Testland",
        jurisdiction_type="country",
        country_code="XX",
    )
    session.add(jurisdiction)
    session.flush()
    authority = Authority(
        jurisdiction_id=jurisdiction.id,
        name="Test Immigration",
        short_name="TI",
        website_url="https://www.example.gov/",
    )
    session.add(authority)
    session.flush()

    broad_url = "https://www.example.gov/work"
    narrow_url = "https://www.example.gov/work/hsm"
    broad = Source(
        authority_id=authority.id,
        jurisdiction_id=jurisdiction.id,
        name="Broad work permits",
        seed_url=broad_url,
        seed_url_normalized=normalize_url(broad_url),
        allowed_url_patterns=[path_prefix_pattern(broad_url)],
        review_status="approved",
        default_language="en",
    )
    narrow = Source(
        authority_id=authority.id,
        jurisdiction_id=jurisdiction.id,
        name="Narrow HSM",
        seed_url=narrow_url,
        seed_url_normalized=normalize_url(narrow_url),
        allowed_url_patterns=[path_prefix_pattern(narrow_url)],
        review_status="approved",
        default_language="en",
    )
    session.add_all([broad, narrow])
    session.flush()
    return broad, narrow


def test_shared_url_preserves_primary_source_ownership(db_session, tmp_path: Path):
    _clear_registry(db_session)
    broad, narrow = _overlapping_sources(db_session)

    # Narrow source owns the HSM seed document first.
    narrow_seed = fetch_approved_seed(
        db_session,
        narrow.id,
        persist=True,
        http_client=_mock_client(html=b"<html>hsm seed</html>"),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    assert narrow_seed.document_id is not None
    owned = db_session.get(Document, narrow_seed.document_id)
    assert owned is not None
    assert owned.primary_source_id == narrow.id

    # Broad source later fetches the same URL via discovery-style fetch.
    reused = fetch_html_document_url(
        db_session,
        broad,
        url=narrow.seed_url_normalized,
        discovery_status="discovered",
        persist=True,
        http_client=_mock_client(html=b"<html>hsm via broad</html>"),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()

    assert reused.status == STATUS_FETCHED_NEW_VERSION
    assert reused.document_id == owned.id
    db_session.refresh(owned)
    assert owned.primary_source_id == narrow.id
    assert owned.discovery_status == "seed"
    # Attribution for this fetch lives on the version row.
    version = db_session.get(DocumentVersion, reused.version_id)
    assert version is not None
    assert version.fetched_via_source_id == broad.id
    assert db_session.scalar(select(func.count()).select_from(Document)) == 1


def test_discover_uses_selected_source_allowlist_not_document_owner(
    db_session, tmp_path: Path
):
    _clear_registry(db_session)
    broad, narrow = _overlapping_sources(db_session)

    # Broad seed HTML links to nested HSM URL and a sibling under /work/.
    broad_html = b"""
    <html><body>
      <a href="/work/hsm">HSM</a>
      <a href="/work/start-up">Start-up</a>
      <a href="/work/hsm/details">HSM details</a>
    </body></html>
    """
    broad_seed = fetch_approved_seed(
        db_session,
        broad.id,
        persist=True,
        http_client=_mock_client(html=broad_html),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()

    # Narrow owns the HSM document; HTML also includes an out-of-narrow link.
    narrow_html = b"""
    <html><body>
      <a href="/work/hsm">self</a>
      <a href="/work/hsm/details">details</a>
      <a href="/work/start-up">sibling</a>
    </body></html>
    """
    narrow_seed = fetch_approved_seed(
        db_session,
        narrow.id,
        persist=True,
        http_client=_mock_client(html=narrow_html),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    hsm_doc = db_session.get(Document, narrow_seed.document_id)
    assert hsm_doc is not None
    assert hsm_doc.primary_source_id == narrow.id

    # Broad discovery may fetch the shared HSM URL but must not steal ownership.
    broad_report = discover_from_source_seed(
        db_session,
        broad.id,
        persist=True,
        policy=fetcher_policy(max_discovered_html_fetches_per_run=10),
        http_client=_mock_client(html=b"<html>child</html>"),
        storage_root=tmp_path,
    )
    db_session.flush()
    assert broad_report.source_id == broad.id
    db_session.refresh(hsm_doc)
    assert hsm_doc.primary_source_id == narrow.id
    fetched_urls = {r.seed_url for r in broad_report.fetch_results}
    assert "https://www.example.gov/work/hsm" in fetched_urls
    assert "https://www.example.gov/work/start-up" in fetched_urls

    # Corrupt ownership to simulate the old bug. Selected crawl source must
    # still govern allowlist filtering (no broader-scope inheritance).
    hsm_doc.primary_source_id = broad.id
    db_session.flush()

    # Scan the broad seed page while explicitly crawling as the narrow source.
    via_doc = discover_from_document(
        db_session,
        broad_seed.document_id,
        crawl_source_id=narrow.id,
        persist=False,
        policy=fetcher_policy(),
        storage_root=tmp_path,
    )
    assert via_doc.source_id == narrow.id
    via_urls = {link.url_normalized for link in via_doc.in_scope_html}
    assert "https://www.example.gov/work/hsm" in via_urls
    assert "https://www.example.gov/work/hsm/details" in via_urls
    assert "https://www.example.gov/work/start-up" not in via_urls

    # discover_from_source_seed always passes the selected source, not owner.
    narrow_report = discover_from_source_seed(
        db_session,
        narrow.id,
        persist=False,
        policy=fetcher_policy(),
        storage_root=tmp_path,
    )
    assert narrow_report.source_id == narrow.id
    assert narrow_report.parent_document_id == hsm_doc.id
