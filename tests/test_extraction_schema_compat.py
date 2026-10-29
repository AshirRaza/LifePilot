"""Regression: stale schema_version must force re-extraction."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select

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
from lifepilot.extract.runner import extract_document_version
from lifepilot.extract.schema import SCHEMA_VERSION

SAMPLE_HTML = b"""<!DOCTYPE html><html><head><title>Visa</title></head>
<body><main id="content"><h1>Visa</h1><p>Eligible workers may apply.</p></main></body></html>"""


def _clear(session) -> None:
    session.execute(delete(DocumentContentComparison))
    session.execute(delete(DocumentExtraction))
    session.execute(delete(DocumentVersion))
    session.execute(delete(Document))
    session.execute(delete(Source))
    session.execute(delete(Authority))
    session.execute(delete(Jurisdiction))
    session.flush()


def test_stale_schema_version_rewrites_extraction(db_session, tmp_path: Path):
    _clear(db_session)
    country = Jurisdiction(
        code="GB-SCHEMA",
        name="United Kingdom Schema",
        jurisdiction_type="country",
        country_code="GB",
    )
    db_session.add(country)
    db_session.flush()
    authority = Authority(jurisdiction_id=country.id, name="HO Schema")
    db_session.add(authority)
    db_session.flush()
    source = Source(
        jurisdiction_id=country.id,
        authority_id=authority.id,
        name="schema src",
        seed_url="https://www.gov.uk/schema-test",
        seed_url_normalized="https://www.gov.uk/schema-test",
        allowed_url_patterns=["https://www.gov.uk/schema-test*"],
        review_status="approved",
        default_language="en",
    )
    db_session.add(source)
    db_session.flush()
    document = Document(
        authority_id=authority.id,
        primary_source_id=source.id,
        canonical_url=source.seed_url,
        url_normalized=source.seed_url_normalized,
        content_kind="html_page",
        language="en",
        discovery_status="seed",
    )
    db_session.add(document)
    db_session.flush()
    rel = f"raw/documents/{document.id}/v1.html"
    write_bytes_atomic(tmp_path / rel, SAMPLE_HTML)
    version = DocumentVersion(
        document_id=document.id,
        fetched_via_source_id=source.id,
        version_number=1,
        source_url=source.seed_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime.now(timezone.utc),
        http_status=200,
        content_sha256="a" * 64,
        byte_size=len(SAMPLE_HTML),
        storage_path=rel,
        is_current=True,
    )
    db_session.add(version)
    db_session.flush()

    first = extract_document_version(
        db_session, version, document, persist=True, storage_root=tmp_path
    )
    assert first.status == "success"
    path = tmp_path / first.storage_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    # Simulate stale on-disk schema with identical content hash.
    payload["schema_version"] = 1
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    second = extract_document_version(
        db_session, version, document, persist=True, storage_root=tmp_path
    )
    assert second.status == "success"
    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["schema_version"] == SCHEMA_VERSION
    row = db_session.scalar(select(DocumentExtraction))
    assert row is not None
    assert row.id == first.extraction_id
