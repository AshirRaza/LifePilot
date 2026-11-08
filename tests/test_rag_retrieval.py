"""Tests for Phase 8E internal hybrid retrieval and truncation safety."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

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
from lifepilot.rag.chunker import E5_SAFE_MAX_CHARS, chunk_structured_document
from lifepilot.rag.embedder import FakeEmbedder
from lifepilot.rag.retrieval import (
    InternalKnowledgeRetriever,
    InternalQuery,
    RetrievalStatus,
    reciprocal_rank_fusion,
)


def _sha(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


def _seed_chunk(
    session: Session,
    *,
    code: str,
    text_value: str,
    heading: str | None = "Eligibility",
    url_suffix: str = "page",
) -> DocumentChunk:
    jur = session.scalar(select(Jurisdiction).where(Jurisdiction.code == code))
    if jur is None:
        jur = Jurisdiction(
            code=code,
            name=f"Country {code}",
            jurisdiction_type="supranational" if code == "EU" else "country",
            country_code=None if code == "EU" else code,
        )
        session.add(jur)
        session.flush()
    auth = Authority(jurisdiction_id=jur.id, name=f"Auth {code}-{url_suffix}")
    session.add(auth)
    session.flush()
    source = Source(
        authority_id=auth.id,
        jurisdiction_id=jur.id,
        name=f"Source {code}-{url_suffix}",
        seed_url=f"https://example.test/{code.lower()}/{url_suffix}-seed",
        seed_url_normalized=f"https://example.test/{code.lower()}/{url_suffix}-seed",
        review_status="approved",
    )
    session.add(source)
    session.flush()
    doc = Document(
        authority_id=auth.id,
        primary_source_id=source.id,
        canonical_url=f"https://example.test/{code.lower()}/{url_suffix}",
        url_normalized=f"https://example.test/{code.lower()}/{url_suffix}",
        content_kind="html_page",
        language="en",
        discovery_status="seed",
    )
    session.add(doc)
    session.flush()
    ver = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=source.id,
        version_number=1,
        source_url=doc.canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime.now(timezone.utc),
        http_status=200,
        content_sha256=_sha(f"raw-{code}-{url_suffix}"),
        byte_size=10,
        storage_path=f"raw/documents/{doc.id}/v1.html",
        is_current=True,
    )
    session.add(ver)
    session.flush()
    chunk = DocumentChunk(
        document_id=doc.id,
        document_version_id=ver.id,
        source_id=source.id,
        jurisdiction_id=jur.id,
        jurisdiction_code=code,
        chunk_index=0,
        content_text=text_value,
        content_sha256=_sha(text_value),
        is_current_version=True,
        heading_path=heading,
    )
    session.add(chunk)
    session.flush()
    return chunk


def _embed_chunk(session: Session, chunk: DocumentChunk, embedder: FakeEmbedder) -> None:
    vec = embedder.embed_passages([chunk.content_text]).vectors[0]
    session.add(
        DocumentChunkEmbedding(
            chunk_id=chunk.id,
            embedding_model_id=PLANNED_EMBEDDING_MODEL_ID,
            embedding=list(map(float, vec.tolist())),
        )
    )
    session.flush()


def test_rrf_deterministic_and_dedupes():
    fused = reciprocal_rank_fusion([[3, 1, 2], [2, 3, 4]], k=60)
    ids = [i for i, _ in fused]
    assert len(ids) == len(set(ids))
    assert ids[0] in (2, 3)
    # Stable tie-break by id when scores equal
    a = reciprocal_rank_fusion([[1, 2], [2, 1]], k=60)
    b = reciprocal_rank_fusion([[1, 2], [2, 1]], k=60)
    assert a == b


def test_chunker_respects_e5_safe_max_chars():
    long_para = ("Salary thresholds and sponsorship rules. " * 80).strip()
    assert len(long_para) > E5_SAFE_MAX_CHARS
    drafts = chunk_structured_document(
        {
            "title": "Guidance",
            "blocks": [
                {"type": "heading", "level": 1, "text": "Eligibility"},
                {"type": "paragraph", "text": long_para},
            ],
        },
        min_chars=40,
    )
    assert drafts
    assert all(len(d.content_text) <= E5_SAFE_MAX_CHARS for d in drafts)
    joined = " ".join(d.content_text for d in drafts)
    assert "Salary thresholds" in joined


def test_retrieval_requires_jurisdiction(db_session: Session):
    retriever = InternalKnowledgeRetriever(db_session, embedder=FakeEmbedder())
    result = retriever.retrieve(
        InternalQuery(query="salary", jurisdiction="", mode="keyword")
    )
    assert result.status == RetrievalStatus.INVALID_REQUEST


def test_pathway_filter_unsupported_without_associations(db_session: Session):
    _seed_chunk(
        db_session,
        code="GB",
        text_value="Skilled Worker visa salary requirements from a licensed sponsor.",
        url_suffix="sw",
    )
    retriever = InternalKnowledgeRetriever(db_session, embedder=FakeEmbedder())
    result = retriever.retrieve(
        InternalQuery(
            query="salary",
            jurisdiction="GB",
            mode="keyword",
            pathway_id="gb_skilled_worker",
        )
    )
    assert result.status == RetrievalStatus.UNSUPPORTED_FILTER


def test_keyword_and_hybrid_jurisdiction_isolation(db_session: Session):
    gb = _seed_chunk(
        db_session,
        code="GB",
        text_value="Skilled Worker visa salary threshold and Certificate of Sponsorship.",
        url_suffix="gb1",
    )
    ie = _seed_chunk(
        db_session,
        code="IE",
        text_value="Critical Skills Employment Permit salary threshold requirements.",
        url_suffix="ie1",
    )
    eu = _seed_chunk(
        db_session,
        code="EU",
        text_value="EU Blue Card highly qualified employment salary conditions.",
        url_suffix="eu1",
    )
    fake = FakeEmbedder()
    for chunk in (gb, ie, eu):
        _embed_chunk(db_session, chunk, fake)

    # Ensure generated FTS column is populated (migration).
    db_session.execute(text("SELECT search_tsv FROM document_chunks LIMIT 1"))

    retriever = InternalKnowledgeRetriever(db_session, embedder=fake)
    kw = retriever.retrieve(
        InternalQuery(query="salary threshold", jurisdiction="GB", mode="keyword", top_k=5)
    )
    assert kw.status == RetrievalStatus.OK
    assert kw.passages
    assert all(p.jurisdiction_code == "GB" for p in kw.passages)
    assert all(p.verification_state == "unverified_retrieval" for p in kw.passages)
    assert all("not validated pathway evidence" in p.applicability_warning for p in kw.passages)

    hy = retriever.retrieve(
        InternalQuery(query="salary threshold", jurisdiction="GB", mode="hybrid", top_k=5)
    )
    assert hy.status == RetrievalStatus.OK
    assert all(p.jurisdiction_code == "GB" for p in hy.passages)

    eu_hit = retriever.retrieve(
        InternalQuery(query="Blue Card", jurisdiction="EU", mode="keyword", top_k=5)
    )
    assert eu_hit.status == RetrievalStatus.OK
    assert all(p.jurisdiction_code == "EU" for p in eu_hit.passages)


def test_semantic_current_version_only(db_session: Session):
    current = _seed_chunk(
        db_session,
        code="NL",
        text_value="Highly skilled migrant residence permit income requirement.",
        url_suffix="nl-cur",
    )
    stale = _seed_chunk(
        db_session,
        code="NL",
        text_value="Outdated highly skilled migrant guidance that must not appear.",
        url_suffix="nl-old",
    )
    stale.is_current_version = False
    db_session.flush()
    fake = FakeEmbedder()
    _embed_chunk(db_session, current, fake)
    _embed_chunk(db_session, stale, fake)

    retriever = InternalKnowledgeRetriever(db_session, embedder=fake)
    result = retriever.retrieve(
        InternalQuery(
            query="highly skilled migrant income",
            jurisdiction="NL",
            mode="semantic",
            top_k=5,
        )
    )
    assert result.status == RetrievalStatus.OK
    assert all(p.chunk_id != stale.id for p in result.passages)
    assert any(p.chunk_id == current.id for p in result.passages)


def test_empty_results(db_session: Session):
    retriever = InternalKnowledgeRetriever(db_session, embedder=FakeEmbedder())
    result = retriever.retrieve(
        InternalQuery(
            query="zzznomatchtokenxyz",
            jurisdiction="JP",
            mode="keyword",
            top_k=5,
        )
    )
    assert result.status == RetrievalStatus.NO_RESULTS
    assert result.passages == []
