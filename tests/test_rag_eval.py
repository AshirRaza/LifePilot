"""Essential Phase 8F/8G evaluation helper tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from lifepilot.db.models import (
    Authority,
    Document,
    DocumentChunk,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.rag.eval import (
    _needle_from_excerpt,
    _needles_from_excerpt,
    _norm,
    _recall_at,
    _first_relevant_rank,
    resolve_passage_references,
)


def test_norm_and_needle():
    assert _norm("  Hello\nWorld  ") == "hello world"
    needle = _needle_from_excerpt(
        "To qualify for a Skilled Worker visa, you must: work for an approved UK sponsor."
    )
    assert needle is not None
    assert "skilled worker" in needle
    assert _needle_from_excerpt("too short") is None


def test_needles_prefer_longer_spans():
    excerpt = (
        "Requirements — recognised sponsor employer must apply; "
        "extra requirements for researchers/doctors."
    )
    needles = _needles_from_excerpt(excerpt)
    assert needles
    assert len(needles[0]) >= len(needles[-1])
    assert any("recognised sponsor" in n for n in needles)


def test_recall_and_mrr_helpers():
    relevant = {10, 20}
    assert _recall_at(3, [1, 10, 3], relevant) == 1.0
    assert _recall_at(3, [1, 2, 3], relevant) == 0.0
    assert _first_relevant_rank([5, 20, 10], relevant) == 2
    assert _first_relevant_rank([1, 2, 3], relevant) is None


def _sha(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


def test_resolve_passage_references_substring_and_adjacent(db_session: Session):
    jur = Jurisdiction(code="ZZ", name="Testland", jurisdiction_type="country", country_code="ZZ")
    db_session.add(jur)
    db_session.flush()
    auth = Authority(jurisdiction_id=jur.id, name="Auth ZZ")
    db_session.add(auth)
    db_session.flush()
    source = Source(
        authority_id=auth.id,
        jurisdiction_id=jur.id,
        name="Source ZZ",
        seed_url="https://example.test/zz",
        seed_url_normalized="https://example.test/zz",
        review_status="approved",
    )
    db_session.add(source)
    db_session.flush()
    doc = Document(
        authority_id=auth.id,
        primary_source_id=source.id,
        canonical_url="https://example.test/zz/page",
        url_normalized="https://example.test/zz/page",
        content_kind="html_page",
        language="en",
        discovery_status="seed",
    )
    db_session.add(doc)
    db_session.flush()
    ver = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=source.id,
        version_number=1,
        source_url=doc.canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime.now(timezone.utc),
        http_status=200,
        content_sha256=_sha("raw"),
        byte_size=10,
        storage_path=f"raw/documents/{doc.id}/v1.html",
        is_current=True,
    )
    db_session.add(ver)
    db_session.flush()
    texts = [
        "Alpha introduction about skilled migrant eligibility.",
        "Recognised sponsor employer must apply for the permit.",
        "Extra requirements for researchers and doctors follow next.",
    ]
    chunks = []
    for i, text_value in enumerate(texts):
        chunk = DocumentChunk(
            document_id=doc.id,
            document_version_id=ver.id,
            source_id=source.id,
            jurisdiction_id=jur.id,
            jurisdiction_code="ZZ",
            chunk_index=i,
            content_text=text_value,
            content_sha256=_sha(text_value),
            is_current_version=True,
        )
        db_session.add(chunk)
        db_session.flush()
        chunks.append(chunk)

    hits, method = resolve_passage_references(
        db_session,
        document_ids=[doc.id],
        excerpts=["Recognised sponsor employer must apply for the permit."],
    )
    assert method == "substring"
    assert hits == frozenset({chunks[1].id})

    # Adjacent span across chunk boundary.
    adj_hits, adj_method = resolve_passage_references(
        db_session,
        document_ids=[doc.id],
        excerpts=[
            "Recognised sponsor employer must apply for the permit. "
            "Extra requirements for researchers and doctors"
        ],
    )
    assert adj_method in {"substring", "adjacent"}
    assert chunks[1].id in adj_hits

    unresolved, unresolved_method = resolve_passage_references(
        db_session,
        document_ids=[doc.id],
        excerpts=["Completely unrelated passage about lunar mining licenses."],
    )
    assert unresolved_method == "unresolved"
    assert unresolved == frozenset()
