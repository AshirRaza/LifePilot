"""Mocked tests for Phase 4D PDF/DOCX discovery and fetch."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy import delete, func, select

from lifepilot.crawl.binary_fetch import fetch_binary_url
from lifepilot.crawl.binary_validate import (
    validate_docx_bytes,
    validate_pdf_bytes,
)
from lifepilot.crawl.documents_discover import discover_binaries_for_source
from lifepilot.crawl.fetcher import (
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_FETCHED_NEW_VERSION,
    STATUS_SKIPPED,
    STATUS_UNCHANGED,
    fetch_approved_seed,
    fetch_binary_document_url,
)
from lifepilot.crawl.links import discover_links_from_html, resolve_and_classify_link
from lifepilot.crawl.policy import fetcher_policy
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
from lifepilot.registry.urls import normalize_url, path_prefix_pattern


def _clear_registry(session) -> None:
    session.execute(delete(DocumentExtraction))
    session.execute(delete(DocumentVersion))
    session.execute(delete(Document))
    session.execute(delete(Source))
    session.execute(delete(Authority))
    session.execute(delete(Jurisdiction))
    session.flush()


def _approve_gov_uk(session) -> Source:
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


def _minimal_docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            (
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                'package/2006/content-types">'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'wordprocessingml.document.main+xml"/></Types>'
            ),
        )
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>',
        )
    return buf.getvalue()


def _stream_response(
    *,
    status_code: int = 200,
    headers: dict | None = None,
    body: bytes = b"%PDF-1.4 sample",
):
    stream = MagicMock()
    stream.status_code = status_code
    stream.headers = headers or {"content-type": "application/pdf"}
    stream.iter_bytes.side_effect = lambda: iter([body])
    stream.close = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = stream
    cm.__exit__.return_value = False
    return cm


def _mock_client(
    *,
    body: bytes,
    content_type: str,
    robots_text: str = "User-agent: *\nAllow: /\n",
    stream_side_effect=None,
) -> MagicMock:
    client = MagicMock()
    client.get.return_value = MagicMock(status_code=200, text=robots_text)
    if stream_side_effect is not None:
        client.stream.side_effect = stream_side_effect
    else:
        client.stream.return_value = _stream_response(
            headers={"content-type": content_type},
            body=body,
        )
    return client


def test_link_extraction_pdf_docx_and_dedupe():
    patterns = ["https://www.gov.uk/skilled-worker-visa*"]
    html = """
    <html><body>
      <a href="/skilled-worker-visa/a.pdf">A</a>
      <a href="/skilled-worker-visa/a.pdf?download=1">A2</a>
      <a href="/skilled-worker-visa/guide.docx">D</a>
      <a href="/browse/other.pdf">out</a>
    </body></html>
    """
    links = discover_links_from_html(
        html,
        base_url="https://www.gov.uk/skilled-worker-visa",
        allowed_patterns=patterns,
    )
    kinds = {link.kind for link in links}
    assert "in_scope_pdf" in kinds
    assert "in_scope_docx" in kinds
    assert "out_of_scope" in kinds
    # Query variant normalizes separately if query preserved.
    pdfs = [link for link in links if link.kind == "in_scope_pdf"]
    assert len(pdfs) >= 1


def test_validate_pdf_and_reject_html_disguise():
    assert validate_pdf_bytes(b"%PDF-1.7\n...").ok is True
    bad = validate_pdf_bytes(b"<!DOCTYPE html><html>Radware</html>")
    assert bad.ok is False
    assert "html" in bad.reason


def test_validate_docx_package_and_reject_plain_zip():
    ok = validate_docx_bytes(_minimal_docx_bytes(), max_uncompressed_bytes=1_000_000)
    assert ok.ok is True
    assert ok.content_kind == "docx"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "not a docx")
    bad = validate_docx_bytes(buf.getvalue(), max_uncompressed_bytes=1_000_000)
    assert bad.ok is False


def test_binary_fetch_blocks_private_redirect():
    policy = fetcher_policy()
    patterns = ["https://www.gov.uk/skilled-worker-visa*"]
    redirect = _stream_response(
        status_code=302,
        headers={"location": "http://127.0.0.1/secret.pdf"},
        body=b"",
    )
    client = MagicMock()
    client.get.return_value = MagicMock(
        status_code=200, text="User-agent: *\nAllow: /\n"
    )
    client.stream.return_value = redirect
    result = fetch_binary_url(
        "https://www.gov.uk/skilled-worker-visa/a.pdf",
        expected_kind="pdf",
        allowed_patterns=patterns,
        policy=policy,
        client=client,
    )
    assert result.ok is False
    assert "blocked host" in (result.error or "") or "allowlist" in (result.error or "")


def test_binary_fetch_robots_disallow():
    policy = fetcher_policy()
    patterns = ["https://www.gov.uk/skilled-worker-visa*"]
    client = _mock_client(
        body=b"%PDF-1.4",
        content_type="application/pdf",
        robots_text="User-agent: *\nDisallow: /\n",
    )
    result = fetch_binary_url(
        "https://www.gov.uk/skilled-worker-visa/a.pdf",
        expected_kind="pdf",
        allowed_patterns=patterns,
        policy=policy,
        client=client,
    )
    assert result.ok is False
    assert "robots" in (result.error or "")


def test_persist_pdf_creates_version(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_gov_uk(db_session)
    pdf = b"%PDF-1.4\n% LifePilot test\n"
    result = fetch_binary_document_url(
        db_session,
        source,
        url="https://www.gov.uk/skilled-worker-visa/guidance.pdf",
        expected_kind="pdf",
        persist=True,
        http_client=_mock_client(body=pdf, content_type="application/pdf"),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    assert result.status == STATUS_FETCHED_NEW_VERSION
    doc = db_session.get(Document, result.document_id)
    assert doc is not None
    assert doc.content_kind == "pdf"
    assert doc.primary_source_id == source.id
    assert result.storage_path.endswith(".pdf")
    assert (tmp_path / result.storage_path).read_bytes() == pdf


def test_unchanged_pdf_does_not_create_new_version(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_gov_uk(db_session)
    pdf = b"%PDF-1.4 same"
    url = "https://www.gov.uk/skilled-worker-visa/same.pdf"
    client = _mock_client(body=pdf, content_type="application/pdf")
    first = fetch_binary_document_url(
        db_session,
        source,
        url=url,
        expected_kind="pdf",
        persist=True,
        http_client=client,
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    second = fetch_binary_document_url(
        db_session,
        source,
        url=url,
        expected_kind="pdf",
        persist=True,
        http_client=client,
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    assert first.status == STATUS_FETCHED_NEW_VERSION
    assert second.status == STATUS_UNCHANGED
    assert db_session.scalar(select(func.count()).select_from(DocumentVersion)) == 1


def test_changed_pdf_creates_new_version(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_gov_uk(db_session)
    url = "https://www.gov.uk/skilled-worker-visa/changing.pdf"
    r1 = fetch_binary_document_url(
        db_session,
        source,
        url=url,
        expected_kind="pdf",
        persist=True,
        http_client=_mock_client(body=b"%PDF-1.4 one", content_type="application/pdf"),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    r2 = fetch_binary_document_url(
        db_session,
        source,
        url=url,
        expected_kind="pdf",
        persist=True,
        http_client=_mock_client(body=b"%PDF-1.4 two", content_type="application/pdf"),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    assert r1.version_number == 1
    assert r2.version_number == 2
    assert (tmp_path / r1.storage_path).read_bytes() == b"%PDF-1.4 one"
    assert (tmp_path / r2.storage_path).read_bytes() == b"%PDF-1.4 two"


def test_html_challenge_disguised_as_pdf_not_stored(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_gov_uk(db_session)
    result = fetch_binary_document_url(
        db_session,
        source,
        url="https://www.gov.uk/skilled-worker-visa/fake.pdf",
        expected_kind="pdf",
        persist=True,
        http_client=_mock_client(
            body=b"<!DOCTYPE html><title>Radware Page</title>",
            content_type="application/pdf",
        ),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    assert result.status == STATUS_FAILED
    assert db_session.scalar(select(func.count()).select_from(DocumentVersion)) == 0


def test_oversized_binary_rejected(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_gov_uk(db_session)
    policy = fetcher_policy(max_binary_response_bytes=100)
    big = b"%PDF-1.4 " + (b"x" * 200)
    result = fetch_binary_document_url(
        db_session,
        source,
        url="https://www.gov.uk/skilled-worker-visa/big.pdf",
        expected_kind="pdf",
        persist=True,
        http_client=_mock_client(body=big, content_type="application/pdf"),
        storage_root=tmp_path,
        policy=policy,
    )
    assert result.status == STATUS_FAILED
    assert "max_binary_response_bytes" in result.message


def test_candidate_source_skipped_for_binary(db_session, tmp_path: Path):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(select(Source).limit(1))
    assert source is not None
    assert source.review_status == "candidate"
    result = fetch_binary_document_url(
        db_session,
        source,
        url=source.seed_url_normalized + "/x.pdf",
        expected_kind="pdf",
        persist=True,
        http_client=_mock_client(body=b"%PDF-1.4", content_type="application/pdf"),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    assert result.status == STATUS_SKIPPED


def test_shared_binary_url_preserves_ownership(db_session, tmp_path: Path):
    _clear_registry(db_session)
    jurisdiction = Jurisdiction(
        code="XX", name="Testland", jurisdiction_type="country", country_code="XX"
    )
    db_session.add(jurisdiction)
    db_session.flush()
    authority = Authority(
        jurisdiction_id=jurisdiction.id, name="Auth", website_url="https://www.example.gov/"
    )
    db_session.add(authority)
    db_session.flush()
    broad_url = "https://www.example.gov/work"
    narrow_url = "https://www.example.gov/work/hsm"
    broad = Source(
        authority_id=authority.id,
        jurisdiction_id=jurisdiction.id,
        name="Broad",
        seed_url=broad_url,
        seed_url_normalized=normalize_url(broad_url),
        allowed_url_patterns=[path_prefix_pattern(broad_url)],
        review_status="approved",
        default_language="en",
    )
    narrow = Source(
        authority_id=authority.id,
        jurisdiction_id=jurisdiction.id,
        name="Narrow",
        seed_url=narrow_url,
        seed_url_normalized=normalize_url(narrow_url),
        allowed_url_patterns=[path_prefix_pattern(narrow_url)],
        review_status="approved",
        default_language="en",
    )
    db_session.add_all([broad, narrow])
    db_session.flush()

    pdf_url = "https://www.example.gov/work/hsm/guide.pdf"
    pdf = b"%PDF-1.4 owned"
    first = fetch_binary_document_url(
        db_session,
        narrow,
        url=pdf_url,
        expected_kind="pdf",
        persist=True,
        http_client=_mock_client(body=pdf, content_type="application/pdf"),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    second = fetch_binary_document_url(
        db_session,
        broad,
        url=pdf_url,
        expected_kind="pdf",
        persist=True,
        http_client=_mock_client(body=b"%PDF-1.4 via broad", content_type="application/pdf"),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    doc = db_session.get(Document, first.document_id)
    assert doc is not None
    assert doc.primary_source_id == narrow.id
    assert second.document_id == doc.id
    version = db_session.get(DocumentVersion, second.version_id)
    assert version is not None
    assert version.fetched_via_source_id == broad.id


def test_discover_binaries_dry_run_no_writes(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_gov_uk(db_session)
    seed_html = b"""
    <html><body>
      <a href="/skilled-worker-visa/a.pdf">PDF</a>
      <a href="/skilled-worker-visa/b.docx">DOCX</a>
      <a href="/other.pdf">out</a>
    </body></html>
    """
    fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=_mock_client(
            body=seed_html, content_type="text/html; charset=utf-8"
        ),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    before = db_session.scalar(select(func.count()).select_from(DocumentVersion))

    report = discover_binaries_for_source(
        db_session,
        source.id,
        persist=False,
        policy=fetcher_policy(max_discovered_binary_fetches_per_run=3),
        storage_root=tmp_path,
        include_discovered_html=False,
    )
    db_session.flush()
    assert len(report.in_scope_pdf) == 1
    assert len(report.in_scope_docx) == 1
    assert len(report.out_of_scope_documents) == 1
    assert report.fetch_results
    assert all(r.status == STATUS_DRY_RUN for r in report.fetch_results)
    assert db_session.scalar(select(func.count()).select_from(DocumentVersion)) == before


def test_discover_binaries_persist_pdf(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_gov_uk(db_session)
    seed_html = b"""
    <html><body>
      <a href="/skilled-worker-visa/pack.pdf">PDF</a>
    </body></html>
    """
    # Seed HTML fetch uses HTML stream; binary uses PDF stream — side_effect.
    html_cm = _stream_response(
        headers={"content-type": "text/html; charset=utf-8"},
        body=seed_html,
    )
    pdf_cm = _stream_response(
        headers={"content-type": "application/pdf"},
        body=b"%PDF-1.4 pack",
    )
    client = MagicMock()
    client.get.return_value = MagicMock(
        status_code=200, text="User-agent: *\nAllow: /\n"
    )
    client.stream.side_effect = [html_cm, pdf_cm]

    fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=client,
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()

    # Fresh client for binary discovery persist.
    report = discover_binaries_for_source(
        db_session,
        source.id,
        persist=True,
        policy=fetcher_policy(max_discovered_binary_fetches_per_run=3),
        http_client=_mock_client(body=b"%PDF-1.4 pack", content_type="application/pdf"),
        storage_root=tmp_path,
        include_discovered_html=False,
    )
    db_session.flush()
    assert any(r.status == STATUS_FETCHED_NEW_VERSION for r in report.fetch_results)
    pdf_docs = list(
        db_session.scalars(select(Document).where(Document.content_kind == "pdf")).all()
    )
    assert len(pdf_docs) == 1
    assert pdf_docs[0].primary_source_id == source.id


def test_out_of_scope_binary_link_classified():
    link = resolve_and_classify_link(
        "https://www.gov.uk/government/uploads/x.pdf",
        base_url="https://www.gov.uk/skilled-worker-visa",
        allowed_patterns=["https://www.gov.uk/skilled-worker-visa*"],
    )
    assert link.kind == "out_of_scope"
