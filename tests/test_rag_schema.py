"""Phase 8B: RAG schema, pgvector, jurisdiction/version safety constraints."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lifepilot.db.models import (
    PLANNED_EMBEDDING_DIMENSIONS,
    PLANNED_EMBEDDING_MODEL_ID,
    Authority,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    DocumentVersion,
    EmbeddingModel,
    Jurisdiction,
    Source,
)


def _sha(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


def _seed_doc_graph(session: Session, *, code: str = "GB") -> dict:
    jur = Jurisdiction(
        code=code,
        name=f"Jurisdiction {code}",
        jurisdiction_type="supranational" if code == "EU" else "country",
        country_code=None if code == "EU" else code,
    )
    session.add(jur)
    session.flush()
    auth = Authority(jurisdiction_id=jur.id, name=f"Authority {code}")
    session.add(auth)
    session.flush()
    source = Source(
        authority_id=auth.id,
        jurisdiction_id=jur.id,
        name=f"Source {code}",
        seed_url=f"https://example.test/{code.lower()}/seed",
        seed_url_normalized=f"https://example.test/{code.lower()}/seed",
        allowed_url_patterns=[f"https://example.test/{code.lower()}/*"],
        review_status="approved",
    )
    session.add(source)
    session.flush()
    doc = Document(
        authority_id=auth.id,
        primary_source_id=source.id,
        canonical_url=f"https://example.test/{code.lower()}/page",
        url_normalized=f"https://example.test/{code.lower()}/page",
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
        content_sha256=_sha("raw"),
        byte_size=3,
        storage_path=f"data/raw/documents/{doc.id}/v1.html",
        is_current=True,
    )
    session.add(ver)
    session.flush()
    return {
        "jurisdiction": jur,
        "authority": auth,
        "source": source,
        "document": doc,
        "version": ver,
    }


def test_pgvector_extension_enabled(db_session: Session):
    row = db_session.execute(
        text("SELECT extname, extversion FROM pg_extension WHERE extname='vector'")
    ).one()
    assert row.extname == "vector"
    assert row.extversion


def test_planned_embedding_model_seeded(db_session: Session):
    model = db_session.get(EmbeddingModel, PLANNED_EMBEDDING_MODEL_ID)
    assert model is not None
    assert model.dimensions == PLANNED_EMBEDDING_DIMENSIONS


def test_chunk_requires_jurisdiction(db_session: Session):
    g = _seed_doc_graph(db_session)
    chunk = DocumentChunk(
        document_id=g["document"].id,
        document_version_id=g["version"].id,
        source_id=g["source"].id,
        jurisdiction_id=g["jurisdiction"].id,
        jurisdiction_code="",  # empty — rejected by trigger/check
        chunk_index=0,
        content_text="Eligible applicants need a licensed sponsor.",
        content_sha256=_sha("Eligible applicants need a licensed sponsor."),
        is_current_version=True,
    )
    db_session.add(chunk)
    with pytest.raises(Exception):
        db_session.flush()
    db_session.rollback()


def test_chunk_jurisdiction_code_must_match_id(db_session: Session):
    g = _seed_doc_graph(db_session, code="GB")
    other = Jurisdiction(
        code="US",
        name="United States",
        jurisdiction_type="country",
        country_code="US",
    )
    db_session.add(other)
    db_session.flush()
    chunk = DocumentChunk(
        document_id=g["document"].id,
        document_version_id=g["version"].id,
        source_id=g["source"].id,
        jurisdiction_id=g["jurisdiction"].id,
        jurisdiction_code="US",  # mismatch vs GB id
        chunk_index=0,
        content_text="Skilled Worker visa guidance for applicants.",
        content_sha256=_sha("Skilled Worker visa guidance for applicants."),
        is_current_version=True,
    )
    db_session.add(chunk)
    with pytest.raises(Exception):
        db_session.flush()
    db_session.rollback()


def test_chunk_insert_with_matching_jurisdiction(db_session: Session):
    g = _seed_doc_graph(db_session, code="EU")
    body = "EU Blue Card directive contextual text for residence."
    chunk = DocumentChunk(
        document_id=g["document"].id,
        document_version_id=g["version"].id,
        source_id=g["source"].id,
        jurisdiction_id=g["jurisdiction"].id,
        jurisdiction_code="EU",
        pathway_id=None,
        requirement_category="eligibility",
        heading_path="Eligibility > Conditions",
        char_start=0,
        char_end=len(body),
        chunk_index=0,
        content_text=body,
        content_sha256=_sha(body),
        is_current_version=True,
    )
    db_session.add(chunk)
    db_session.flush()
    assert chunk.id is not None
    assert chunk.is_current_version is True


def test_invalid_requirement_category_rejected(db_session: Session):
    g = _seed_doc_graph(db_session)
    body = "Some substantive immigration guidance text for testing."
    chunk = DocumentChunk(
        document_id=g["document"].id,
        document_version_id=g["version"].id,
        source_id=g["source"].id,
        jurisdiction_id=g["jurisdiction"].id,
        jurisdiction_code="GB",
        requirement_category="not_a_real_requirement",
        chunk_index=0,
        content_text=body,
        content_sha256=_sha(body),
        is_current_version=True,
    )
    db_session.add(chunk)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_embedding_fk_and_384d_roundtrip(db_session: Session):
    g = _seed_doc_graph(db_session)
    body = "Graduate visa applicants must have completed an eligible course."
    chunk = DocumentChunk(
        document_id=g["document"].id,
        document_version_id=g["version"].id,
        source_id=g["source"].id,
        jurisdiction_id=g["jurisdiction"].id,
        jurisdiction_code="GB",
        chunk_index=0,
        content_text=body,
        content_sha256=_sha(body),
        is_current_version=True,
    )
    db_session.add(chunk)
    db_session.flush()
    vector = [0.01] * PLANNED_EMBEDDING_DIMENSIONS
    emb = DocumentChunkEmbedding(
        chunk_id=chunk.id,
        embedding_model_id=PLANNED_EMBEDDING_MODEL_ID,
        embedding=vector,
    )
    db_session.add(emb)
    db_session.flush()
    loaded = db_session.get(DocumentChunkEmbedding, emb.id)
    assert loaded is not None
    assert len(loaded.embedding) == PLANNED_EMBEDDING_DIMENSIONS
    assert loaded.embedding_model_id == PLANNED_EMBEDDING_MODEL_ID


def test_non_384_model_cannot_use_embeddings_table(db_session: Session):
    g = _seed_doc_graph(db_session)
    body = "Student visa funds and course confirmation requirements."
    chunk = DocumentChunk(
        document_id=g["document"].id,
        document_version_id=g["version"].id,
        source_id=g["source"].id,
        jurisdiction_id=g["jurisdiction"].id,
        jurisdiction_code="GB",
        chunk_index=0,
        content_text=body,
        content_sha256=_sha(body),
        is_current_version=True,
    )
    db_session.add(chunk)
    db_session.flush()
    db_session.add(
        EmbeddingModel(
            id="fake/other-768",
            display_name="Fake 768",
            dimensions=768,
            notes="Must not write into vector(384) table",
        )
    )
    db_session.flush()
    emb = DocumentChunkEmbedding(
        chunk_id=chunk.id,
        embedding_model_id="fake/other-768",
        embedding=[0.0] * PLANNED_EMBEDDING_DIMENSIONS,
    )
    db_session.add(emb)
    with pytest.raises(Exception):
        db_session.flush()
    db_session.rollback()


def test_chunk_fk_to_missing_document_rejected(db_session: Session):
    g = _seed_doc_graph(db_session)
    body = "Foreign key integrity check for document reference."
    chunk = DocumentChunk(
        document_id=9_999_999,
        document_version_id=g["version"].id,
        source_id=g["source"].id,
        jurisdiction_id=g["jurisdiction"].id,
        jurisdiction_code="GB",
        chunk_index=0,
        content_text=body,
        content_sha256=_sha(body),
        is_current_version=True,
    )
    db_session.add(chunk)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_retrievable_filter_index_exists(db_session: Session):
    rows = db_session.execute(
        text(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'document_chunks'
              AND indexname = 'ix_document_chunks_retrievable'
            """
        )
    ).fetchall()
    assert rows


def test_url_normalize_localhost_to_ipv4():
    from lifepilot.db.url import normalize_database_url

    out = normalize_database_url(
        "postgresql+psycopg://u:p@localhost:5432/lifepilot"
    )
    assert "@127.0.0.1:5432/" in out
    assert "localhost" not in out.split("@", 1)[1]
