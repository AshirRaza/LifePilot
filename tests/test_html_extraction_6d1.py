"""Phase 6D.1: government HTML fixtures, empty-retry policy, version upgrades."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select

from lifepilot.crawl.storage import write_bytes_atomic
from lifepilot.db.models import (
    Authority,
    Document,
    DocumentContentComparison,
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
    STATUS_EMPTY,
    STATUS_SUCCESS,
    STATUS_UNCHANGED,
    extract_document_version,
)
from lifepilot.extract.schema import SCHEMA_VERSION
from lifepilot.ingest.pipeline import (
    MODE_PROCESS_EXISTING,
    IngestLimits,
    _needs_extraction,
    run_ingestion,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "html"

MOM_HTML = (FIXTURES / "mom_employment_pass.html").read_bytes()
USCIS_HTML = (FIXTURES / "uscis_working_in_us.html").read_bytes()
AU_HTML = (FIXTURES / "au_skilled_migration.html").read_bytes()
DETE_HTML = (FIXTURES / "ie_dete_employment_permits.html").read_bytes()
AU_PAGESCHEMA_HTML = (
    FIXTURES / "au_sharepoint_pageschema_empty_main.html"
).read_bytes()

EMPTY_MAIN_HTML = b"""<!DOCTYPE html>
<html><head><title>Empty page</title></head>
<body><main id="content"><div class="placeholder"></div></main></body></html>
"""

MINIMAL_SUCCESS_HTML = (
    b"<html><body><main><h1>X</h1><p>Y</p></main></body></html>"
)


def _clear(session) -> None:
    session.execute(delete(DocumentContentComparison))
    session.execute(delete(DocumentExtraction))
    session.execute(delete(DocumentVersion))
    session.execute(delete(Document))
    session.execute(delete(Source))
    session.execute(delete(Authority))
    session.execute(delete(Jurisdiction))
    session.flush()


def _seed_source(session, *, name: str = "src") -> Source:
    country = Jurisdiction(
        code=f"XX-{name[:6]}",
        name="Exampleland",
        jurisdiction_type="country",
        country_code="XX",
    )
    session.add(country)
    session.flush()
    authority = Authority(jurisdiction_id=country.id, name=f"Auth {name}")
    session.add(authority)
    session.flush()
    source = Source(
        authority_id=authority.id,
        jurisdiction_id=country.id,
        name=name,
        seed_url=f"https://example.test/{name}",
        seed_url_normalized=f"https://example.test/{name}",
        review_status="approved",
    )
    session.add(source)
    session.flush()
    return source


def _add_doc_version(
    session,
    tmp_path: Path,
    source: Source,
    *,
    html: bytes,
    url: str,
    raw_sha: str = "ab" * 32,
) -> tuple[Document, DocumentVersion]:
    doc = Document(
        authority_id=source.authority_id,
        primary_source_id=source.id,
        canonical_url=url,
        url_normalized=url,
        content_kind="html_page",
        language="en",
        discovery_status="discovered",
    )
    session.add(doc)
    session.flush()
    rel = f"raw/documents/{doc.id}/v1.html"
    write_bytes_atomic(tmp_path / rel, html)
    version = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=source.id,
        version_number=1,
        source_url=url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime.now(timezone.utc),
        http_status=200,
        content_sha256=raw_sha,
        byte_size=len(html),
        storage_path=rel,
        is_current=True,
    )
    session.add(version)
    session.flush()
    return doc, version


def test_mom_form_wrapped_main_extracts_headings_and_lists():
    doc = extract_html_document(MOM_HTML)
    assert "Employment Pass" in doc.title
    assert len(doc.blocks) >= 10
    types = {b.type for b in doc.blocks}
    assert "heading" in types
    assert "paragraph" in types
    texts = " ".join(b.text or "" for b in doc.blocks if b.text)
    assert "foreign professionals" in texts.lower() or "Employment Pass" in texts


def test_uscis_feedback_wrapper_does_not_wipe_main():
    doc = extract_html_document(USCIS_HTML)
    assert "Working in the United States" in doc.title
    assert len(doc.blocks) >= 5
    blob = json.dumps(doc.to_content_dict())
    assert "Was this page helpful" not in blob
    assert any(
        b.type == "heading" and "Working in the United States" in (b.text or "")
        for b in doc.blocks
    )


def test_australia_aspnet_form_preserves_main():
    doc = extract_html_document(AU_HTML)
    assert "Skilled migration" in doc.title
    assert len(doc.blocks) >= 3
    assert any(b.type == "heading" for b in doc.blocks)
    assert any(b.type == "paragraph" for b in doc.blocks)


def test_australia_sharepoint_pageschema_empty_main_recovers_guidance():
    """Home Affairs visa pages leave <main> empty; guidance is in hidden JSON."""
    doc = extract_html_document(AU_PAGESCHEMA_HTML)
    assert "Student visa" in (doc.title or "")
    assert len(doc.blocks) >= 6
    types = {b.type for b in doc.blocks}
    assert "heading" in types
    assert "paragraph" in types
    assert "list" in types
    texts = " ".join(
        (b.text or "") + " " + " ".join(b.items or ()) for b in doc.blocks
    )
    assert "Eligibility" in texts
    assert "Genuine Temporary Entrant" in texts
    assert "ImmiAccount" in texts
    blob = json.dumps(doc.to_content_dict()).lower()
    # siteData menu / alert noise must not become the document body
    assert "site alert noise" not in blob
    assert "home affairs menu" not in blob
    assert "copyright commonwealth" not in blob


def test_embedded_json_dedupes_exact_duplicate_blocks():
    """Mirrored CMS fragments should not repeat identical blocks."""
    import html as html_mod

    payload = {
        "title": "Example visa",
        "sectionA": "<h2>Eligibility</h2><p>You must meet the criteria.</p><ul><li>Passport</li></ul>",
        "sectionB": "<h2>Eligibility</h2><p>You must meet the criteria.</p><ul><li>Passport</li></ul>",
        "sectionC": "<h2>Fees</h2><p>Check the pricing estimator.</p>",
    }
    escaped = html_mod.escape(json.dumps(payload), quote=True)
    html = f"""<!DOCTYPE html>
<html><head><title>Example visa</title></head>
<body>
<main id="content"></main>
<input type="hidden" value="{escaped}" />
</body></html>
""".encode()
    doc = extract_html_document(html)
    headings = [b.text for b in doc.blocks if b.type == "heading"]
    assert headings.count("Eligibility") == 1
    assert headings.count("Fees") == 1
    assert any(
        b.type == "paragraph" and "pricing estimator" in (b.text or "").lower()
        for b in doc.blocks
    )


def test_embedded_json_recovers_thin_hub_description_and_eligibility():
    """Citizenship/visa hubs with only description+eligibility HTML still extract."""
    import html as html_mod

    payload = {
        "title": "Skills in Demand visa",
        "description": (
            "<p>This temporary visa lets an employer sponsor a skilled worker.</p>"
        ),
        "eligibility": (
            "<p>You must:</p><ul><li>Have an approved sponsor</li>"
            "<li>Be nominated for the visa</li></ul>"
        ),
        "recommended": {"cards": [{"id": "x-1", "itemName": "x-1"}]},
    }
    escaped = html_mod.escape(json.dumps(payload), quote=True)
    html = f"""<!DOCTYPE html><html><head><title>Skills in Demand visa</title></head>
<body><main id="content"></main>
<input type="hidden" value="{escaped}" /></body></html>""".encode()
    doc = extract_html_document(html)
    assert "Skills in Demand" in (doc.title or "")
    assert len(doc.blocks) >= 2
    blob = " ".join(
        (b.text or "") + " " + " ".join(b.items or ()) for b in doc.blocks
    ).lower()
    assert "approved sponsor" in blob
    assert "temporary visa" in blob


def test_embedded_json_recovers_tile_catalog_on_thin_main():
    """Working-hub tile JSON becomes pathway list when main is sparse."""
    import html as html_mod

    payload = {
        "tiles": {
            "tiles": [
                {
                    "title": "Regional migration",
                    "description": "Work in regional Australia",
                    "link": "/visas/working-in-australia/regional-migration",
                },
                {
                    "title": "Temporary work visas",
                    "description": "See temporary work options",
                    "link": "/visas/working-in-australia/temporary-work-visas",
                },
                {
                    "title": "Permanent work visas",
                    "description": "See permanent work options",
                    "link": "/visas/working-in-australia/permanent-work-visas",
                },
            ]
        }
    }
    escaped = html_mod.escape(json.dumps(payload), quote=True)
    html = f"""<!DOCTYPE html><html><head><title>Working in Australia</title></head>
<body><main id="content"><h1>Working in Australia</h1>
<p>Choose a visa that suits the work you intend to do.</p></main>
<input type="hidden" value="{escaped}" /></body></html>""".encode()
    doc = extract_html_document(html)
    assert len(doc.blocks) >= 4
    blob = " ".join(
        (b.text or "") + " " + " ".join(b.items or ()) for b in doc.blocks
    )
    assert "Regional migration" in blob
    assert "Temporary work visas" in blob
    assert "Permanent work visas" in blob


def test_embedded_json_fallback_does_not_degrade_existing_fixtures():
    """Regression: pages with real <main> content keep the DOM path."""
    for label, raw, min_blocks in (
        ("mom", MOM_HTML, 10),
        ("uscis", USCIS_HTML, 5),
        ("au_skilled", AU_HTML, 3),
        ("dete", DETE_HTML, 5),
    ):
        doc = extract_html_document(raw)
        assert len(doc.blocks) >= min_blocks, label
        assert any(b.type == "heading" for b in doc.blocks), label
        assert any(b.type == "paragraph" for b in doc.blocks), label
    uk_html = b"""<!DOCTYPE html><html><head><title>Skilled Worker visa - GOV.UK</title></head>
<body><main id="content"><h1>Skilled Worker visa</h1>
<p>Work in the UK for an approved employer.</p>
<h2>Eligibility</h2><ul><li>Job offer</li><li>Skill level</li></ul>
</main></body></html>"""
    uk = extract_html_document(uk_html)
    assert "Skilled Worker visa" in uk.title
    assert len(uk.blocks) >= 3


def test_ireland_dete_aspnet_form_preserves_article():
    doc = extract_html_document(DETE_HTML)
    assert "Employment Permits" in doc.title or "permits" in doc.title.lower()
    assert len(doc.blocks) >= 5
    assert any(b.type == "heading" for b in doc.blocks)
    assert any(b.type == "paragraph" for b in doc.blocks)


def test_fixture_content_hash_stable():
    a = extract_html_document(MOM_HTML)
    b = extract_html_document(MOM_HTML)
    assert normalized_content_sha256(a) == normalized_content_sha256(b)
    assert len(normalized_content_sha256(a)) == 64


def test_empty_extraction_not_retried_same_extractor_version(
    db_session, tmp_path: Path
):
    _clear(db_session)
    source = _seed_source(db_session, name="empty-src")
    doc, version = _add_doc_version(
        db_session,
        tmp_path,
        source,
        html=EMPTY_MAIN_HTML,
        url="https://example.test/empty",
    )
    first = extract_document_version(
        db_session, version, doc, persist=True, storage_root=tmp_path
    )
    assert first.status == STATUS_EMPTY
    assert first.extractor_version == EXTRACTOR_VERSION
    raw_before = (tmp_path / version.storage_path).read_bytes()

    assert _needs_extraction(
        db_session, version, doc, storage_root=tmp_path
    ) is False

    report = run_ingestion(
        db_session,
        execute=True,
        mode=MODE_PROCESS_EXISTING,
        source_id=source.id,
        limits=IngestLimits(max_extractions=5, max_comparisons=0),
        storage_root=tmp_path,
        write_report_file=False,
    )
    assert report.extractions_empty == 0
    assert report.extractions_success == 0
    assert report.extraction_backlog_remaining == 0
    # Historical raw untouched; empty derivation not rewritten repeatedly.
    assert (tmp_path / version.storage_path).read_bytes() == raw_before
    second = extract_document_version(
        db_session, version, doc, persist=True, storage_root=tmp_path
    )
    assert second.status == STATUS_UNCHANGED


def test_extractor_version_upgrade_retries_empty(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session, name="upgrade-src")
    doc, version = _add_doc_version(
        db_session,
        tmp_path,
        source,
        html=MOM_HTML,
        url="https://example.test/mom",
        raw_sha="cd" * 32,
    )
    # Simulate prior empty result from older extractor.
    empty_path = f"extracted/documents/{doc.id}/v1.html_structured.json"
    write_bytes_atomic(
        tmp_path / empty_path,
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "extractor": EXTRACTOR_NAME,
                "extractor_version": "1.1.0",
                "title": "Employment Pass",
                "blocks": [],
                "content_sha256": "0" * 64,
                "source_url": version.source_url,
                "document_id": doc.id,
                "document_version_id": version.id,
            },
            indent=2,
        ).encode("utf-8"),
    )
    row = DocumentExtraction(
        document_version_id=version.id,
        extractor_name=EXTRACTOR_NAME,
        extractor_version="1.1.0",
        status="empty",
        content_sha256="0" * 64,
        storage_path=empty_path,
        title="Employment Pass",
        message="no content blocks extracted",
        extracted_at=datetime.now(timezone.utc),
    )
    db_session.add(row)
    db_session.flush()

    raw_before = (tmp_path / version.storage_path).read_bytes()
    assert _needs_extraction(db_session, version, doc, storage_root=tmp_path) is True

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
    assert refreshed.status == "success"
    assert refreshed.extractor_version == EXTRACTOR_VERSION
    stored = json.loads((tmp_path / refreshed.storage_path).read_text(encoding="utf-8"))
    assert len(stored["blocks"]) >= 10
    assert stored["schema_version"] == SCHEMA_VERSION
    # Raw historical file preserved.
    assert (tmp_path / version.storage_path).read_bytes() == raw_before
    assert (tmp_path / version.storage_path).read_bytes() == MOM_HTML


def test_failed_extraction_remains_eligible(db_session, tmp_path: Path):
    _clear(db_session)
    source = _seed_source(db_session, name="fail-src")
    doc, version = _add_doc_version(
        db_session,
        tmp_path,
        source,
        html=MINIMAL_SUCCESS_HTML,
        url="https://example.test/fail",
    )
    row = DocumentExtraction(
        document_version_id=version.id,
        extractor_name=EXTRACTOR_NAME,
        extractor_version=EXTRACTOR_VERSION,
        status="failed",
        content_sha256=None,
        storage_path=None,
        title=None,
        message="extraction failed: boom",
        extracted_at=datetime.now(timezone.utc),
    )
    db_session.add(row)
    db_session.flush()
    assert _needs_extraction(db_session, version, doc, storage_root=tmp_path) is True
    result = extract_document_version(
        db_session, version, doc, persist=True, storage_root=tmp_path
    )
    assert result.status == STATUS_SUCCESS
    assert db_session.scalar(select(func.count()).select_from(DocumentExtraction)) == 1
