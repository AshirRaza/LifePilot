"""Phase 5C meaningful content comparison tests (mocked fixtures only)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import delete, func, select

from lifepilot.crawl.storage import extraction_file_path, write_bytes_atomic
from lifepilot.db.models import (
    Authority,
    Document,
    DocumentContentComparison,
    DocumentExtraction,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.extract.compare import (
    STATUS_CHANGED,
    STATUS_UNAVAILABLE,
    STATUS_UNCHANGED,
    diff_structured_documents,
)
from lifepilot.extract.compare_runner import (
    compare_batch,
    compare_document_latest,
    compare_version_pair,
    persist_batch,
    persist_comparison,
)
from lifepilot.extract.html_structured import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    extract_html_document,
)
from lifepilot.extract.schema import (
    SCHEMA_VERSION,
    ContentBlock,
    StructuredDocument,
    normalized_content_sha256,
)
from lifepilot.extract.runner import extract_document_version


def _clear(session) -> None:
    session.execute(delete(DocumentContentComparison))
    session.execute(delete(DocumentExtraction))
    session.execute(delete(DocumentVersion))
    session.execute(delete(Document))
    session.execute(delete(Source))
    session.execute(delete(Authority))
    session.execute(delete(Jurisdiction))
    session.flush()


def _seed_source(session) -> Source:
    country = Jurisdiction(
        code="GB-CMP",
        name="United Kingdom",
        jurisdiction_type="country",
        country_code="GB",
    )
    session.add(country)
    session.flush()
    authority = Authority(jurisdiction_id=country.id, name="Home Office")
    session.add(authority)
    session.flush()
    source = Source(
        jurisdiction_id=country.id,
        authority_id=authority.id,
        name="cmp_test",
        seed_url="https://www.gov.uk/cmp-test",
        seed_url_normalized="https://www.gov.uk/cmp-test",
        allowed_url_patterns=["https://www.gov.uk/cmp-test*"],
        default_language="en",
        review_status="approved",
    )
    session.add(source)
    session.flush()
    return source


def _write_extraction(
    session,
    tmp_path: Path,
    *,
    document: Document,
    version: DocumentVersion,
    structured: StructuredDocument,
    extractor_name: str = EXTRACTOR_NAME,
    extractor_version: str = EXTRACTOR_VERSION,
    status: str = "success",
) -> DocumentExtraction:
    digest = normalized_content_sha256(structured) if status == "success" else None
    abs_path = extraction_file_path(
        tmp_path,
        document.id,
        version.version_number,
        extractor_name=extractor_name,
    )
    rel = abs_path.relative_to(tmp_path).as_posix()
    if status == "success":
        payload = structured.to_storage_dict(
            extractor=extractor_name,
            extractor_version=extractor_version,
            document_id=document.id,
            document_version_id=version.id,
            source_url=version.source_url,
            content_sha256=digest,
            schema_version=SCHEMA_VERSION,
        )
        write_bytes_atomic(
            abs_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode(
                "utf-8"
            ),
        )
    row = DocumentExtraction(
        document_version_id=version.id,
        extractor_name=extractor_name,
        extractor_version=extractor_version,
        status=status,
        content_sha256=digest,
        storage_path=rel if status == "success" else None,
        title=structured.title if status == "success" else None,
        extracted_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def _add_version(
    session,
    tmp_path: Path,
    document: Document,
    source: Source,
    *,
    version_number: int,
    raw_bytes: bytes,
    raw_sha: str,
    is_current: bool,
) -> DocumentVersion:
    ext = "html" if document.content_kind == "html_page" else document.content_kind
    rel = f"raw/documents/{document.id}/v{version_number}.{ext}"
    write_bytes_atomic(tmp_path / rel, raw_bytes)
    version = DocumentVersion(
        document_id=document.id,
        fetched_via_source_id=source.id,
        version_number=version_number,
        source_url=document.canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime.now(timezone.utc),
        http_status=200,
        content_sha256=raw_sha,
        byte_size=len(raw_bytes),
        storage_path=rel,
        is_current=is_current,
    )
    session.add(version)
    session.flush()
    return version


def _doc_with_two_versions(
    session,
    tmp_path: Path,
    *,
    prev_html: bytes,
    curr_html: bytes,
    prev_raw_sha: str = "1" * 64,
    curr_raw_sha: str = "2" * 64,
    url: str = "https://www.gov.uk/cmp-test/page",
) -> tuple[Document, DocumentVersion, DocumentVersion, Source]:
    source = _seed_source(session)
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
    v1 = _add_version(
        session,
        tmp_path,
        document,
        source,
        version_number=1,
        raw_bytes=prev_html,
        raw_sha=prev_raw_sha,
        is_current=False,
    )
    v2 = _add_version(
        session,
        tmp_path,
        document,
        source,
        version_number=2,
        raw_bytes=curr_html,
        raw_sha=curr_raw_sha,
        is_current=True,
    )
    return document, v1, v2, source


HTML_BASE = b"""<!DOCTYPE html><html><head><title>Visa</title></head>
<body><main id="content">
<h1>Visa</h1>
<p>You may apply if eligible.</p>
<h2>Eligibility</h2>
<p>You must have a job offer.</p>
<ul><li>Sponsor licence</li><li>English language</li></ul>
<h2>Fees</h2>
<table><thead><tr><th>Item</th><th>Amount</th></tr></thead>
<tbody><tr><td>Application</td><td>719</td></tr></tbody></table>
</main></body></html>"""

HTML_WHITESPACE = b"""<!DOCTYPE html><html><head><title>Visa</title></head>
<body><main id="content">
<h1>Visa</h1>
<p>You   may   apply
if eligible.</p>
<h2>Eligibility</h2>
<p>You must have a job offer.</p>
<ul><li>Sponsor licence</li><li>English language</li></ul>
<h2>Fees</h2>
<table><thead><tr><th>Item</th><th>Amount</th></tr></thead>
<tbody><tr><td>Application</td><td>719</td></tr></tbody></table>
</main></body></html>"""

HTML_ELIGIBILITY_CHANGED = b"""<!DOCTYPE html><html><head><title>Visa</title></head>
<body><main id="content">
<h1>Visa</h1>
<p>You may apply if eligible.</p>
<h2>Eligibility</h2>
<p>You must have a job offer lasting at least 6 months.</p>
<ul><li>Sponsor licence</li><li>English language</li></ul>
<h2>Fees</h2>
<table><thead><tr><th>Item</th><th>Amount</th></tr></thead>
<tbody><tr><td>Application</td><td>719</td></tr></tbody></table>
</main></body></html>"""

HTML_FEE_CHANGED = b"""<!DOCTYPE html><html><head><title>Visa</title></head>
<body><main id="content">
<h1>Visa</h1>
<p>You may apply if eligible.</p>
<h2>Eligibility</h2>
<p>You must have a job offer.</p>
<ul><li>Sponsor licence</li><li>English language</li></ul>
<h2>Fees</h2>
<table><thead><tr><th>Item</th><th>Amount</th></tr></thead>
<tbody><tr><td>Application</td><td>884</td></tr></tbody></table>
</main></body></html>"""

HTML_HEADING_ADDED = b"""<!DOCTYPE html><html><head><title>Visa</title></head>
<body><main id="content">
<h1>Visa</h1>
<p>You may apply if eligible.</p>
<h2>Eligibility</h2>
<p>You must have a job offer.</p>
<ul><li>Sponsor licence</li><li>English language</li></ul>
<h2>Documents</h2>
<p>Provide your passport.</p>
<h2>Fees</h2>
<table><thead><tr><th>Item</th><th>Amount</th></tr></thead>
<tbody><tr><td>Application</td><td>719</td></tr></tbody></table>
</main></body></html>"""

HTML_LIST_CHANGED = b"""<!DOCTYPE html><html><head><title>Visa</title></head>
<body><main id="content">
<h1>Visa</h1>
<p>You may apply if eligible.</p>
<h2>Eligibility</h2>
<p>You must have a job offer.</p>
<ul><li>Sponsor licence</li><li>English language</li><li>Maintenance funds</li></ul>
<h2>Fees</h2>
<table><thead><tr><th>Item</th><th>Amount</th></tr></thead>
<tbody><tr><td>Application</td><td>719</td></tr></tbody></table>
</main></body></html>"""


def _persist_pair(session, tmp_path, document, v1, v2, html1, html2):
    s1 = extract_html_document(html1)
    s2 = extract_html_document(html2)
    _write_extraction(session, tmp_path, document=document, version=v1, structured=s1)
    _write_extraction(session, tmp_path, document=document, version=v2, structured=s2)
    return s1, s2


def test_raw_differs_extracted_identical_is_unchanged(db_session, tmp_path: Path):
    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session,
        tmp_path,
        prev_html=HTML_BASE,
        curr_html=HTML_WHITESPACE,
        prev_raw_sha="a" * 64,
        curr_raw_sha="b" * 64,
    )
    _persist_pair(db_session, tmp_path, document, v1, v2, HTML_BASE, HTML_WHITESPACE)
    result = compare_version_pair(
        db_session,
        previous_version_id=v1.id,
        current_version_id=v2.id,
        storage_root=tmp_path,
    )
    assert result.status == STATUS_UNCHANGED
    assert result.raw_sha256_equal is False


def test_eligibility_wording_changed(db_session, tmp_path: Path):
    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session, tmp_path, prev_html=HTML_BASE, curr_html=HTML_ELIGIBILITY_CHANGED
    )
    _persist_pair(
        db_session, tmp_path, document, v1, v2, HTML_BASE, HTML_ELIGIBILITY_CHANGED
    )
    result = compare_version_pair(
        db_session,
        previous_version_id=v1.id,
        current_version_id=v2.id,
        storage_root=tmp_path,
    )
    assert result.status == STATUS_CHANGED
    ops = [c["op"] for c in result.diff["changes"]]
    assert "modified" in ops
    assert any(
        c["block_type"] == "paragraph" and "6 months" in (c["current"] or {}).get("text", "")
        for c in result.diff["changes"]
        if c["op"] == "modified"
    )


def test_fee_amount_change_detected(db_session, tmp_path: Path):
    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session, tmp_path, prev_html=HTML_BASE, curr_html=HTML_FEE_CHANGED
    )
    _persist_pair(db_session, tmp_path, document, v1, v2, HTML_BASE, HTML_FEE_CHANGED)
    result = compare_version_pair(
        db_session,
        previous_version_id=v1.id,
        current_version_id=v2.id,
        storage_root=tmp_path,
    )
    assert result.status == STATUS_CHANGED
    table_changes = [
        c for c in result.diff["changes"] if c["block_type"] == "table"
    ]
    assert table_changes
    detail = table_changes[0].get("detail") or []
    assert any(
        d.get("op") == "modified_cell" and d.get("current") == "884" for d in detail
    )


def test_heading_added_and_list_item_added(db_session, tmp_path: Path):
    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session, tmp_path, prev_html=HTML_BASE, curr_html=HTML_HEADING_ADDED
    )
    _persist_pair(db_session, tmp_path, document, v1, v2, HTML_BASE, HTML_HEADING_ADDED)
    heading_result = compare_version_pair(
        db_session,
        previous_version_id=v1.id,
        current_version_id=v2.id,
        storage_root=tmp_path,
    )
    assert heading_result.status == STATUS_CHANGED
    assert any(
        c["op"] == "added" and c["block_type"] == "heading" and c["current"]["text"] == "Documents"
        for c in heading_result.diff["changes"]
    )

    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session, tmp_path, prev_html=HTML_BASE, curr_html=HTML_LIST_CHANGED
    )
    _persist_pair(db_session, tmp_path, document, v1, v2, HTML_BASE, HTML_LIST_CHANGED)
    list_result = compare_version_pair(
        db_session,
        previous_version_id=v1.id,
        current_version_id=v2.id,
        storage_root=tmp_path,
    )
    assert list_result.status == STATUS_CHANGED
    list_mod = next(c for c in list_result.diff["changes"] if c["block_type"] == "list")
    assert list_mod["op"] == "modified"
    assert any(
        d["op"] == "added_item" and d["text"] == "Maintenance funds"
        for d in list_mod["detail"]
    )


def test_layout_only_page_difference_is_unchanged():
    prev = StructuredDocument(
        title="Guide",
        blocks=[
            ContentBlock(type="paragraph", text="Hello", page=1),
            ContentBlock(type="paragraph", text="World", page=1),
        ],
        page_count=1,
    )
    curr = StructuredDocument(
        title="Guide",
        blocks=[
            ContentBlock(type="paragraph", text="Hello", page=2),
            ContentBlock(type="paragraph", text="World", page=3),
        ],
        page_count=3,
        empty_pages=(1,),
    )
    # Full content hash includes page → differs
    assert normalized_content_sha256(prev) != normalized_content_sha256(curr)
    diff = diff_structured_documents(prev, curr)
    assert diff["status"] == STATUS_UNCHANGED
    assert diff["layout_only"] is True
    assert diff["layout_differences"]
    assert diff["changes"] == []


def test_pdf_page_references_on_changed_block():
    prev = StructuredDocument(
        title="Form",
        blocks=[
            ContentBlock(type="heading", level=1, text="Requirements", page=1),
            ContentBlock(type="paragraph", text="Submit by 1 January 2025.", page=1),
        ],
        page_count=1,
    )
    curr = StructuredDocument(
        title="Form",
        blocks=[
            ContentBlock(type="heading", level=1, text="Requirements", page=1),
            ContentBlock(type="paragraph", text="Submit by 1 March 2026.", page=2),
        ],
        page_count=2,
    )
    diff = diff_structured_documents(prev, curr)
    assert diff["status"] == STATUS_CHANGED
    mod = next(c for c in diff["changes"] if c["op"] == "modified")
    assert mod["previous_page"] == 1
    assert mod["current_page"] == 2
    assert mod["section_heading"] == "Requirements"


def test_missing_extraction_unavailable(db_session, tmp_path: Path):
    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session, tmp_path, prev_html=HTML_BASE, curr_html=HTML_BASE
    )
    s1 = extract_html_document(HTML_BASE)
    _write_extraction(db_session, tmp_path, document=document, version=v1, structured=s1)
    result = compare_version_pair(
        db_session,
        previous_version_id=v1.id,
        current_version_id=v2.id,
        storage_root=tmp_path,
    )
    assert result.status == STATUS_UNAVAILABLE
    assert "no html_structured extraction" in (result.unavailable_reason or "")
    assert "current" in (result.unavailable_reason or "")


def test_failed_extraction_unavailable(db_session, tmp_path: Path):
    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session, tmp_path, prev_html=HTML_BASE, curr_html=HTML_ELIGIBILITY_CHANGED
    )
    s1 = extract_html_document(HTML_BASE)
    s2 = extract_html_document(HTML_ELIGIBILITY_CHANGED)
    _write_extraction(db_session, tmp_path, document=document, version=v1, structured=s1)
    _write_extraction(
        db_session,
        tmp_path,
        document=document,
        version=v2,
        structured=s2,
        status="failed",
    )
    result = compare_version_pair(
        db_session,
        previous_version_id=v1.id,
        current_version_id=v2.id,
        storage_root=tmp_path,
    )
    assert result.status == STATUS_UNAVAILABLE
    assert "status='failed'" in (result.unavailable_reason or "")


def test_incompatible_extractor_version_unavailable(db_session, tmp_path: Path):
    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session, tmp_path, prev_html=HTML_BASE, curr_html=HTML_BASE
    )
    s = extract_html_document(HTML_BASE)
    _write_extraction(
        db_session,
        tmp_path,
        document=document,
        version=v1,
        structured=s,
        extractor_version="1.0.0",
    )
    _write_extraction(
        db_session,
        tmp_path,
        document=document,
        version=v2,
        structured=s,
        extractor_version=EXTRACTOR_VERSION,
    )
    result = compare_version_pair(
        db_session,
        previous_version_id=v1.id,
        current_version_id=v2.id,
        storage_root=tmp_path,
    )
    assert result.status == STATUS_UNAVAILABLE
    assert "incompatible" in (result.unavailable_reason or "")


def test_different_documents_rejected(db_session, tmp_path: Path):
    _clear(db_session)
    d1, v1a, v1b, source = _doc_with_two_versions(
        db_session,
        tmp_path,
        prev_html=HTML_BASE,
        curr_html=HTML_BASE,
        url="https://www.gov.uk/cmp-test/a",
    )
    _persist_pair(db_session, tmp_path, d1, v1a, v1b, HTML_BASE, HTML_BASE)
    d2 = Document(
        authority_id=source.authority_id,
        primary_source_id=source.id,
        canonical_url="https://www.gov.uk/cmp-test/b",
        url_normalized="https://www.gov.uk/cmp-test/b",
        content_kind="html_page",
        language="en",
        discovery_status="seed",
    )
    db_session.add(d2)
    db_session.flush()
    v2 = _add_version(
        db_session,
        tmp_path,
        d2,
        source,
        version_number=1,
        raw_bytes=HTML_BASE,
        raw_sha="c" * 64,
        is_current=True,
    )
    s = extract_html_document(HTML_BASE)
    _write_extraction(db_session, tmp_path, document=d2, version=v2, structured=s)
    result = compare_version_pair(
        db_session,
        previous_version_id=v1a.id,
        current_version_id=v2.id,
        storage_root=tmp_path,
    )
    assert result.status == STATUS_UNAVAILABLE
    assert "different documents" in (result.unavailable_reason or "")


def test_dry_run_does_not_persist(db_session, tmp_path: Path):
    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session, tmp_path, prev_html=HTML_BASE, curr_html=HTML_FEE_CHANGED
    )
    _persist_pair(db_session, tmp_path, document, v1, v2, HTML_BASE, HTML_FEE_CHANGED)
    report = compare_batch(
        db_session,
        document_id=document.id,
        storage_root=tmp_path,
    )
    assert report.changed_count == 1
    assert db_session.scalar(select(func.count()).select_from(DocumentContentComparison)) == 0
    assert list(tmp_path.glob("compared/**/*.json")) == []


def test_persist_idempotent(db_session, tmp_path: Path):
    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session, tmp_path, prev_html=HTML_BASE, curr_html=HTML_FEE_CHANGED
    )
    _persist_pair(db_session, tmp_path, document, v1, v2, HTML_BASE, HTML_FEE_CHANGED)
    first = compare_document_latest(db_session, document.id, storage_root=tmp_path)
    first = persist_comparison(db_session, first, storage_root=tmp_path)
    db_session.flush()
    assert first.comparison_id is not None
    assert first.storage_path
    assert (tmp_path / first.storage_path).is_file()

    second = compare_document_latest(db_session, document.id, storage_root=tmp_path)
    second = persist_comparison(db_session, second, storage_root=tmp_path)
    db_session.flush()
    assert second.comparison_id == first.comparison_id
    assert db_session.scalar(select(func.count()).select_from(DocumentContentComparison)) == 1


def test_persist_supersedes_without_overwriting_history(db_session, tmp_path: Path):
    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session, tmp_path, prev_html=HTML_BASE, curr_html=HTML_FEE_CHANGED
    )
    _persist_pair(db_session, tmp_path, document, v1, v2, HTML_BASE, HTML_FEE_CHANGED)
    result = compare_document_latest(db_session, document.id, storage_root=tmp_path)
    result = persist_comparison(db_session, result, storage_root=tmp_path)
    db_session.flush()
    first_id = result.comparison_id
    first_path = result.storage_path
    assert first_id is not None
    assert first_path
    original = (tmp_path / first_path).read_bytes()

    # Simulate a later compatible result that differs from the historical row.
    conflict = result
    conflict.status = STATUS_UNCHANGED
    conflict.previous_content_sha256 = "0" * 64
    conflict.unavailable_reason = None
    out = persist_comparison(db_session, conflict, storage_root=tmp_path)
    db_session.flush()
    assert out.status == STATUS_UNCHANGED
    assert out.comparison_id != first_id
    assert "supersedes comparison_id=" in (out.message or "")
    assert db_session.scalar(select(func.count()).select_from(DocumentContentComparison)) == 2
    # Historical JSON untouched.
    assert (tmp_path / first_path).read_bytes() == original
    hist = db_session.get(DocumentContentComparison, first_id)
    assert hist is not None
    assert hist.result_revision == 1
    latest = db_session.get(DocumentContentComparison, out.comparison_id)
    assert latest is not None
    assert latest.result_revision == 2
    assert latest.supersedes_id == first_id


def test_db_failure_cleans_orphan_comparison_file(db_session, tmp_path: Path):
    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session, tmp_path, prev_html=HTML_BASE, curr_html=HTML_FEE_CHANGED
    )
    _persist_pair(db_session, tmp_path, document, v1, v2, HTML_BASE, HTML_FEE_CHANGED)
    result = compare_document_latest(db_session, document.id, storage_root=tmp_path)

    real_add = db_session.add

    def add_boom(obj):  # type: ignore[no-untyped-def]
        if isinstance(obj, DocumentContentComparison):
            raise RuntimeError("simulated db failure")
        return real_add(obj)

    with patch.object(db_session, "add", side_effect=add_boom):
        with pytest.raises(RuntimeError):
            persist_comparison(db_session, result, storage_root=tmp_path)

    assert list(tmp_path.glob("compared/documents/**/*.json")) == []


def test_latest_two_and_batch(db_session, tmp_path: Path):
    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session, tmp_path, prev_html=HTML_BASE, curr_html=HTML_ELIGIBILITY_CHANGED
    )
    _persist_pair(
        db_session, tmp_path, document, v1, v2, HTML_BASE, HTML_ELIGIBILITY_CHANGED
    )
    latest = compare_document_latest(db_session, document.id, storage_root=tmp_path)
    assert latest.status == STATUS_CHANGED
    batch = compare_batch(db_session, max_documents=5, storage_root=tmp_path)
    assert batch.changed_count >= 1


def test_extract_regression_still_works(db_session, tmp_path: Path):
    """Existing HTML extraction path still persists successfully."""
    _clear(db_session)
    source = _seed_source(db_session)
    document = Document(
        authority_id=source.authority_id,
        primary_source_id=source.id,
        canonical_url="https://www.gov.uk/cmp-test/reg",
        url_normalized="https://www.gov.uk/cmp-test/reg",
        content_kind="html_page",
        language="en",
        discovery_status="seed",
    )
    db_session.add(document)
    db_session.flush()
    version = _add_version(
        db_session,
        tmp_path,
        document,
        source,
        version_number=1,
        raw_bytes=HTML_BASE,
        raw_sha="d" * 64,
        is_current=True,
    )
    result = extract_document_version(
        db_session, version, document, persist=True, storage_root=tmp_path
    )
    assert result.status == "success"
    assert db_session.scalar(select(func.count()).select_from(DocumentExtraction)) == 1


def test_persist_batch_commit_each(db_session, tmp_path: Path):
    _clear(db_session)
    document, v1, v2, _ = _doc_with_two_versions(
        db_session, tmp_path, prev_html=HTML_BASE, curr_html=HTML_FEE_CHANGED
    )
    _persist_pair(db_session, tmp_path, document, v1, v2, HTML_BASE, HTML_FEE_CHANGED)
    report = compare_batch(db_session, document_id=document.id, storage_root=tmp_path)
    out = persist_batch(db_session, report, storage_root=tmp_path, commit_each=True)
    assert out.results[0].comparison_id is not None
