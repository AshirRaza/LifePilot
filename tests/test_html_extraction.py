"""Mocked tests for Phase 5A structured HTML extraction."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select

from lifepilot.crawl.storage import write_bytes_atomic
from lifepilot.db.models import (
    Authority,
    Document,
    DocumentExtraction,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.extract.html_structured import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    extract_html_document,
    normalized_content_sha256,
)
from lifepilot.extract.runner import (
    STATUS_DRY_RUN,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    STATUS_UNCHANGED,
    extract_html_batch,
    extract_html_version,
)


SAMPLE_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head>
  <title>Skilled Worker visa - GOV.UK</title>
  <script>window.track=1</script>
  <style>.x{color:red}</style>
</head>
<body>
  <header class="govuk-header"><nav>Home</nav></header>
  <div id="global-cookie-message" class="govuk-cookie-banner">
    <p>We use cookies to collect information.</p>
  </div>
  <main id="content" class="govuk-main-wrapper">
    <h1>Skilled Worker visa</h1>
    <p>A Skilled Worker visa allows you to work in the UK.</p>
    <h2>Eligibility</h2>
    <ul>
      <li>Approved UK employer</li>
      <li>Certificate of sponsorship</li>
    </ul>
    <h2>Fees</h2>
    <table>
      <caption>Example fees</caption>
      <thead><tr><th>Item</th><th>Amount</th></tr></thead>
      <tbody>
        <tr><td>Application</td><td>719</td></tr>
        <tr><td>IHS</td><td>1035</td></tr>
      </tbody>
    </table>
    <aside class="gem-c-related-navigation">
      <h2>Related content</h2>
      <p>Should be removed</p>
    </aside>
  </main>
  <footer class="govuk-footer"><p>Crown copyright</p></footer>
</body>
</html>
"""

EMPTY_MAIN_HTML = b"""<!DOCTYPE html>
<html><head><title>Empty page - GOV.UK</title></head>
<body>
<header>Nav</header>
<main id="content"></main>
<footer>Foot</footer>
</body></html>
"""

# Minimal IND-like accordion: ul.accordion__content is layout, not a bullet list.
IND_ACCORDION_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head><title>Highly skilled migrant | IND</title></head>
<body>
<main id="content">
  <h1>Highly skilled migrant</h1>
  <p>Only a recognised employer can apply.</p>
  <h2>Requirements</h2>
  <div class="paragraph--accordion">
    <div class="accordion">
      <ul class="accordion__content">
        <li class="accordion-item">
          <a class="accordion-item__heading" role="button">
            <h3>Extra requirements for researchers</h3>
          </a>
          <div class="accordion-item__content">
            <ul>
              <li>Enough income for researchers</li>
              <li>Employment contract with a research institution</li>
            </ul>
          </div>
        </li>
        <li class="accordion-item">
          <a class="accordion-item__heading" role="button">
            <h3>Extra requirements for medical doctors in training</h3>
          </a>
          <div class="accordion-item__content">
            <p>Doctors in training need a recognised training place.</p>
            <ul>
              <li>Valid training agreement</li>
            </ul>
          </div>
        </li>
      </ul>
    </div>
  </div>
  <h2>Process and costs</h2>
  <ol>
    <li>Employer submits application</li>
    <li>IND decides</li>
  </ol>
</main>
</body>
</html>
"""


def _clear(session) -> None:
    session.execute(delete(DocumentExtraction))
    session.execute(delete(DocumentVersion))
    session.execute(delete(Document))
    session.execute(delete(Source))
    session.execute(delete(Authority))
    session.execute(delete(Jurisdiction))
    session.flush()


def _seed_doc(
    session,
    tmp_path: Path,
    *,
    html: bytes = SAMPLE_HTML,
    discovery_status: str = "seed",
    content_kind: str = "html_page",
    url: str = "https://example.test/page",
) -> tuple[Document, DocumentVersion]:
    country = Jurisdiction(
        code="XX-EX",
        name="Exampleland",
        jurisdiction_type="country",
        country_code="XX",
    )
    session.add(country)
    session.flush()
    authority = Authority(jurisdiction_id=country.id, name="Example Authority")
    session.add(authority)
    session.flush()
    source = Source(
        authority_id=authority.id,
        jurisdiction_id=country.id,
        name="Example Source",
        seed_url=url,
        seed_url_normalized=url,
        review_status="approved",
    )
    session.add(source)
    session.flush()
    doc = Document(
        authority_id=authority.id,
        primary_source_id=source.id,
        canonical_url=url,
        url_normalized=url,
        content_kind=content_kind,
        language="en",
        discovery_status=discovery_status,
    )
    session.add(doc)
    session.flush()

    rel = f"raw/documents/{doc.id}/v1.html"
    abs_path = tmp_path / rel
    write_bytes_atomic(abs_path, html)
    version = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=source.id,
        version_number=1,
        source_url=url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime.now(timezone.utc),
        http_status=200,
        content_sha256="a" * 64,
        byte_size=len(html),
        storage_path=rel,
        is_current=True,
    )
    session.add(version)
    session.flush()
    return doc, version


def test_extract_preserves_structure_and_strips_boilerplate():
    doc = extract_html_document(SAMPLE_HTML)
    assert doc.title == "Skilled Worker visa"
    types = [b.type for b in doc.blocks]
    assert types == [
        "heading",
        "paragraph",
        "heading",
        "list",
        "heading",
        "table",
    ]
    assert doc.blocks[0].level == 1
    assert "Skilled Worker visa" in (doc.blocks[0].text or "")
    assert doc.blocks[3].ordered is False
    assert doc.blocks[3].items == (
        "Approved UK employer",
        "Certificate of sponsorship",
    )
    table = doc.blocks[5]
    assert table.caption == "Example fees"
    assert table.headers == ("Item", "Amount")
    assert table.rows == (("Application", "719"), ("IHS", "1035"))

    blob = json.dumps(doc.to_content_dict()).lower()
    assert "cookie" not in blob
    assert "crown copyright" not in blob
    assert "related content" not in blob
    assert "window.track" not in blob


def test_normalized_hash_is_stable():
    a = extract_html_document(SAMPLE_HTML)
    b = extract_html_document(SAMPLE_HTML)
    assert normalized_content_sha256(a) == normalized_content_sha256(b)
    assert len(normalized_content_sha256(a)) == 64


def test_empty_main_yields_no_blocks():
    doc = extract_html_document(EMPTY_MAIN_HTML)
    assert doc.title == "Empty page"
    assert doc.blocks == []


def test_rejected_document_is_skipped(db_session, tmp_path: Path):
    _clear(db_session)
    doc, version = _seed_doc(
        db_session, tmp_path, discovery_status="rejected", html=SAMPLE_HTML
    )
    result = extract_html_version(
        db_session, version, doc, persist=True, storage_root=tmp_path
    )
    assert result.status == STATUS_SKIPPED
    assert "rejected" in result.message
    row = db_session.scalar(select(DocumentExtraction))
    assert row is not None
    assert row.status == "skipped"
    assert row.storage_path is None
    # Raw file untouched.
    assert (tmp_path / version.storage_path).read_bytes() == SAMPLE_HTML


def test_empty_content_persists_empty_status(db_session, tmp_path: Path):
    _clear(db_session)
    doc, version = _seed_doc(db_session, tmp_path, html=EMPTY_MAIN_HTML)
    result = extract_html_version(
        db_session, version, doc, persist=True, storage_root=tmp_path
    )
    assert result.status == STATUS_EMPTY
    assert result.block_count == 0
    row = db_session.scalar(select(DocumentExtraction))
    assert row is not None
    assert row.status == "empty"
    assert row.content_sha256 is not None
    stored = json.loads((tmp_path / row.storage_path).read_text(encoding="utf-8"))
    assert stored["blocks"] == []
    assert stored["extractor"] == EXTRACTOR_NAME
    assert stored["extractor_version"] == EXTRACTOR_VERSION


def test_persist_success_and_idempotent_rerun(db_session, tmp_path: Path):
    _clear(db_session)
    doc, version = _seed_doc(db_session, tmp_path)
    first = extract_html_version(
        db_session, version, doc, persist=True, storage_root=tmp_path
    )
    assert first.status == STATUS_SUCCESS
    assert first.block_count == 6
    assert first.storage_path is not None
    count = db_session.scalar(select(func.count()).select_from(DocumentExtraction))
    assert count == 1
    payload = json.loads((tmp_path / first.storage_path).read_text(encoding="utf-8"))
    assert payload["content_sha256"] == first.content_sha256
    assert payload["title"] == "Skilled Worker visa"

    second = extract_html_version(
        db_session, version, doc, persist=True, storage_root=tmp_path
    )
    assert second.status == STATUS_UNCHANGED
    assert second.extraction_id == first.extraction_id
    count2 = db_session.scalar(select(func.count()).select_from(DocumentExtraction))
    assert count2 == 1
    # Raw immutable.
    assert (tmp_path / version.storage_path).read_bytes() == SAMPLE_HTML


def test_dry_run_does_not_write(db_session, tmp_path: Path):
    _clear(db_session)
    doc, version = _seed_doc(db_session, tmp_path)
    result = extract_html_version(
        db_session, version, doc, persist=False, storage_root=tmp_path
    )
    assert result.status == STATUS_DRY_RUN
    assert db_session.scalar(select(func.count()).select_from(DocumentExtraction)) == 0
    extracted_root = tmp_path / "extracted"
    assert not extracted_root.exists()


def test_missing_file_is_failed_on_persist(db_session, tmp_path: Path):
    _clear(db_session)
    doc, version = _seed_doc(db_session, tmp_path)
    (tmp_path / version.storage_path).unlink()
    result = extract_html_version(
        db_session, version, doc, persist=True, storage_root=tmp_path
    )
    assert result.status == STATUS_FAILED
    row = db_session.scalar(select(DocumentExtraction))
    assert row is not None
    assert row.status == "failed"


def test_batch_respects_max_documents(db_session, tmp_path: Path):
    _clear(db_session)
    _seed_doc(db_session, tmp_path, url="https://example.test/a")
    # Second document needs unique jurisdiction code — use separate seed helper path.
    country = db_session.scalar(select(Jurisdiction))
    authority = db_session.scalar(select(Authority))
    source = db_session.scalar(select(Source))
    assert country and authority and source
    doc2 = Document(
        authority_id=authority.id,
        primary_source_id=source.id,
        canonical_url="https://example.test/b",
        url_normalized="https://example.test/b",
        content_kind="html_page",
        discovery_status="discovered",
    )
    db_session.add(doc2)
    db_session.flush()
    rel = f"raw/documents/{doc2.id}/v1.html"
    write_bytes_atomic(tmp_path / rel, SAMPLE_HTML)
    db_session.add(
        DocumentVersion(
            document_id=doc2.id,
            fetched_via_source_id=source.id,
            version_number=1,
            source_url="https://example.test/b",
            content_type="text/html",
            retrieved_at=datetime.now(timezone.utc),
            http_status=200,
            content_sha256="b" * 64,
            byte_size=len(SAMPLE_HTML),
            storage_path=rel,
            is_current=True,
        )
    )
    db_session.flush()

    report = extract_html_batch(
        db_session,
        persist=True,
        max_documents=1,
        storage_root=tmp_path,
    )
    assert len(report.results) == 1
    assert report.success_count == 1


def test_non_html_content_kind_skipped(db_session, tmp_path: Path):
    _clear(db_session)
    doc, version = _seed_doc(
        db_session,
        tmp_path,
        content_kind="pdf",
        url="https://example.test/file.pdf",
    )
    # Pretend storage is pdf path for skip classification.
    version.storage_path = f"raw/documents/{doc.id}/v1.pdf"
    db_session.flush()
    result = extract_html_version(
        db_session, version, doc, persist=True, storage_root=tmp_path
    )
    assert result.status == STATUS_SKIPPED
    assert "html_page" in result.message


def test_ind_accordion_preserves_nested_headings_and_lists():
    doc = extract_html_document(IND_ACCORDION_HTML)
    types = [b.type for b in doc.blocks]
    assert types == [
        "heading",  # h1
        "paragraph",
        "heading",  # Requirements
        "heading",  # researchers
        "list",
        "heading",  # medical doctors
        "paragraph",
        "list",
        "heading",  # Process and costs
        "list",  # ordinary ordered list
    ]
    assert doc.blocks[3].type == "heading"
    assert doc.blocks[3].level == 3
    assert doc.blocks[3].text == "Extra requirements for researchers"
    assert doc.blocks[4].items == (
        "Enough income for researchers",
        "Employment contract with a research institution",
    )
    assert doc.blocks[5].text == "Extra requirements for medical doctors in training"
    assert doc.blocks[9].ordered is True
    assert doc.blocks[9].items == ("Employer submits application", "IND decides")
    # Accordion shell must not become one giant flattened list item.
    blob = json.dumps(doc.to_content_dict())
    assert "Extra requirements for researchers Enough income" not in blob


def test_ordinary_ul_still_extracted_as_list_block():
    doc = extract_html_document(SAMPLE_HTML)
    lists = [b for b in doc.blocks if b.type == "list"]
    assert len(lists) == 1
    assert lists[0].ordered is False
    assert lists[0].items == (
        "Approved UK employer",
        "Certificate of sponsorship",
    )


def test_accordion_content_order_matches_document_order():
    doc = extract_html_document(IND_ACCORDION_HTML)
    heading_texts = [b.text for b in doc.blocks if b.type == "heading"]
    assert heading_texts == [
        "Highly skilled migrant",
        "Requirements",
        "Extra requirements for researchers",
        "Extra requirements for medical doctors in training",
        "Process and costs",
    ]
    # Nested list after researcher heading, before medical heading.
    researcher_idx = next(
        i for i, b in enumerate(doc.blocks) if b.text == "Extra requirements for researchers"
    )
    medical_idx = next(
        i
        for i, b in enumerate(doc.blocks)
        if b.text == "Extra requirements for medical doctors in training"
    )
    assert doc.blocks[researcher_idx + 1].type == "list"
    assert researcher_idx < medical_idx
