"""Tests for Phase 8C cleaning, chunking, metadata and idempotency."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lifepilot.crawl.storage import data_root, extraction_file_path, relative_storage_path
from lifepilot.db.models import (
    Authority,
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.rag.chunker import chunk_structured_document
from lifepilot.rag.clean import filter_blocks, is_boilerplate_block
from lifepilot.rag.pipeline import chunk_one_version, run_chunking


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_extraction(
    tmp_data: Path,
    *,
    document_id: int,
    version_number: int,
    blocks: list[dict],
    title: str = "Test Guidance",
    extractor: str = "html_structured",
) -> str:
    root = tmp_data
    path = extraction_file_path(
        root, document_id, version_number, extractor_name=extractor
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "extractor": extractor,
        "extractor_version": "test",
        "document_id": document_id,
        "title": title,
        "blocks": blocks,
        "content_sha256": "a" * 64,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return relative_storage_path(root, path)


def _seed(
    session: Session,
    tmp_data: Path,
    *,
    code: str = "GB",
    blocks: list[dict] | None = None,
    is_current: bool = True,
    version_number: int = 1,
) -> dict:
    jur = Jurisdiction(
        code=code,
        name=f"Country {code}",
        jurisdiction_type="supranational" if code == "EU" else "country",
        country_code=None if code == "EU" else code,
    )
    session.add(jur)
    session.flush()
    auth = Authority(jurisdiction_id=jur.id, name=f"Auth {code}")
    session.add(auth)
    session.flush()
    source = Source(
        authority_id=auth.id,
        jurisdiction_id=jur.id,
        name=f"Source {code}",
        seed_url=f"https://example.test/{code.lower()}/seed",
        seed_url_normalized=f"https://example.test/{code.lower()}/seed",
        review_status="approved",
    )
    session.add(source)
    session.flush()
    doc = Document(
        authority_id=auth.id,
        primary_source_id=source.id,
        canonical_url=f"https://example.test/{code.lower()}/page-{version_number}",
        url_normalized=f"https://example.test/{code.lower()}/page-{version_number}",
        content_kind="html_page",
        language="en",
        discovery_status="seed",
    )
    session.add(doc)
    session.flush()
    ver = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=source.id,
        version_number=version_number,
        source_url=doc.canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime.now(timezone.utc),
        http_status=200,
        content_sha256=_sha(f"raw-{code}-{version_number}"),
        byte_size=10,
        storage_path=f"raw/documents/{doc.id}/v{version_number}.html",
        is_current=is_current,
    )
    session.add(ver)
    session.flush()
    default_blocks = blocks or [
        {"type": "heading", "level": 1, "text": "Eligibility"},
        {
            "type": "paragraph",
            "text": (
                "You can apply for this visa if you have a job offer from a licensed "
                "sponsor and meet the skill and salary requirements published by the "
                "authority. You must also satisfy English language conditions."
            ),
        },
        {"type": "heading", "level": 2, "text": "Documents you need"},
        {
            "type": "list",
            "ordered": False,
            "items": [
                "Valid passport",
                "Certificate of sponsorship",
                "Proof of funds covering maintenance for the required period",
            ],
        },
        {"type": "paragraph", "text": "Skip to main content"},
        {"type": "paragraph", "text": "We use cookies to improve your experience."},
    ]
    rel = _write_extraction(
        tmp_data,
        document_id=doc.id,
        version_number=version_number,
        blocks=default_blocks,
    )
    ext = DocumentExtraction(
        document_version_id=ver.id,
        extractor_name="html_structured",
        extractor_version="test",
        status="success",
        content_sha256=_sha("extraction"),
        storage_path=rel,
        title="Test Guidance",
        extracted_at=datetime.now(timezone.utc),
    )
    session.add(ext)
    session.flush()
    return {
        "jurisdiction": jur,
        "source": source,
        "document": doc,
        "version": ver,
        "extraction": ext,
    }


def test_boilerplate_filtered():
    assert is_boilerplate_block({"type": "paragraph", "text": "Skip to main content"})
    assert is_boilerplate_block(
        {"type": "paragraph", "text": "We use cookies to improve your experience."}
    )
    kept = filter_blocks(
        [
            {"type": "paragraph", "text": "Skip to main content"},
            {
                "type": "paragraph",
                "text": "Applicants must hold a valid certificate of sponsorship.",
            },
        ]
    )
    assert len(kept) == 1


def test_chunker_preserves_headings_and_drops_boilerplate():
    payload = {
        "title": "Skilled Worker visa",
        "blocks": [
            {"type": "heading", "level": 1, "text": "Eligibility"},
            {
                "type": "paragraph",
                "text": (
                    "You need a job offer from a licensed sponsor. The role must meet "
                    "the skill level and salary threshold for your occupation code."
                ),
            },
            {"type": "paragraph", "text": "Accept all cookies"},
            {"type": "heading", "level": 2, "text": "Required documents"},
            {
                "type": "paragraph",
                "text": (
                    "Provide your passport, certificate of sponsorship reference, and "
                    "evidence of funds unless you are exempt under the published rules."
                ),
            },
        ],
    }
    drafts = chunk_structured_document(payload, min_chars=40)
    assert drafts
    joined = "\n".join(d.content_text for d in drafts)
    assert "Accept all cookies" not in joined
    assert any(d.heading_path and "Eligibility" in d.heading_path for d in drafts)
    assert all(d.content_sha256 == _sha(d.content_text) for d in drafts)


def test_duplicate_passage_within_document_allowed():
    repeated = (
        "You must provide proof of funds covering the required maintenance period "
        "unless an exemption applies under the official guidance."
    )
    payload = {
        "title": "Funds",
        "blocks": [
            {"type": "heading", "level": 1, "text": "Section A"},
            {"type": "paragraph", "text": repeated},
            {"type": "heading", "level": 1, "text": "Section B"},
            {"type": "paragraph", "text": repeated},
        ],
    }
    drafts = chunk_structured_document(payload, min_chars=40, target_tokens=80, max_tokens=120)
    # May merge or split, but identical hashes must be allowed at draft level.
    assert len(drafts) >= 1


def test_pipeline_writes_jurisdiction_and_skips_superseded(
    db_session: Session, tmp_path: Path
):
    data = tmp_path / "data"
    g = _seed(db_session, data, code="GB")
    old = _seed(
        db_session,
        data,
        code="US",
        is_current=False,
        version_number=1,
        blocks=[
            {"type": "heading", "level": 1, "text": "Old"},
            {
                "type": "paragraph",
                "text": "Superseded US guidance that must not enter active chunks.",
            },
        ],
    )
    # Mark US version non-current explicitly (already False)
    assert old["version"].is_current is False

    stats = run_chunking(db_session, data_dir=data, force=True, batch_size=10)
    assert stats.processed >= 1
    chunks = db_session.scalars(select(DocumentChunk)).all()
    assert chunks
    assert all(c.is_current_version for c in chunks)
    assert all(c.jurisdiction_code for c in chunks)
    assert all(c.pathway_id is None for c in chunks)
    # Superseded version must not be chunked
    superseded_chunks = [
        c for c in chunks if c.document_version_id == old["version"].id
    ]
    assert superseded_chunks == []
    gb_chunks = [c for c in chunks if c.jurisdiction_code == "GB"]
    assert gb_chunks
    assert all(c.document_id == g["document"].id for c in gb_chunks)


def test_pipeline_idempotent_second_run(db_session: Session, tmp_path: Path):
    data = tmp_path / "data"
    _seed(db_session, data, code="NL")
    first = run_chunking(db_session, data_dir=data, force=True)
    count1 = db_session.scalar(select(func.count()).select_from(DocumentChunk)) or 0
    assert first.processed >= 1
    assert count1 > 0
    second = run_chunking(db_session, data_dir=data, force=False)
    count2 = db_session.scalar(select(func.count()).select_from(DocumentChunk)) or 0
    assert second.skipped_unchanged >= 1
    assert count2 == count1


def test_eu_jurisdiction_kept_distinct(db_session: Session, tmp_path: Path):
    data = tmp_path / "data"
    _seed(db_session, data, code="EU")
    run_chunking(db_session, data_dir=data, force=True)
    codes = set(db_session.scalars(select(DocumentChunk.jurisdiction_code)).all())
    assert codes == {"EU"}


def test_chunk_one_version_direct(db_session: Session, tmp_path: Path):
    data = tmp_path / "data"
    g = _seed(db_session, data, code="IE")
    status, written, _deleted = chunk_one_version(
        db_session, g["version"], data_dir=data, force=True
    )
    assert status == "processed"
    assert written >= 1
