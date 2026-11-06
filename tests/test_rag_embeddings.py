"""Offline tests for Phase 8D embedding configuration and pipeline."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import numpy as np
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lifepilot.db.models import (
    PLANNED_EMBEDDING_DIMENSIONS,
    PLANNED_EMBEDDING_MODEL_ID,
    Authority,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.rag.embed_pipeline import run_embedding
from lifepilot.rag.embedder import (
    E5_MODEL_REVISION,
    E5_PASSAGE_PREFIX,
    E5_QUERY_PREFIX,
    EmbedderConfig,
    FakeEmbedder,
    l2_normalize,
    validate_vectors,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_chunk(session: Session, *, code: str = "GB", text: str | None = None) -> DocumentChunk:
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
        seed_url=f"https://example.test/{code.lower()}/seed-emb",
        seed_url_normalized=f"https://example.test/{code.lower()}/seed-emb",
        review_status="approved",
    )
    session.add(source)
    session.flush()
    doc = Document(
        authority_id=auth.id,
        primary_source_id=source.id,
        canonical_url=f"https://example.test/{code.lower()}/emb",
        url_normalized=f"https://example.test/{code.lower()}/emb",
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
        content_sha256=_sha(f"raw-{code}"),
        byte_size=10,
        storage_path=f"raw/documents/{doc.id}/v1.html",
        is_current=True,
    )
    session.add(ver)
    session.flush()
    body = text or (
        "You need a job offer from a licensed sponsor and must meet the "
        "published salary threshold for the occupation."
    )
    chunk = DocumentChunk(
        document_id=doc.id,
        document_version_id=ver.id,
        source_id=source.id,
        jurisdiction_id=jur.id,
        jurisdiction_code=code,
        chunk_index=0,
        content_text=body,
        content_sha256=_sha(body),
        is_current_version=True,
        heading_path="Eligibility",
    )
    session.add(chunk)
    session.flush()
    return chunk


def test_validate_vectors_dimensions_and_norm():
    rng = np.random.default_rng(0)
    raw = rng.normal(size=(3, PLANNED_EMBEDDING_DIMENSIONS)).astype(np.float32)
    vectors = l2_normalize(raw)
    validate_vectors(vectors, dimensions=PLANNED_EMBEDDING_DIMENSIONS)
    with pytest.raises(ValueError):
        validate_vectors(raw, dimensions=PLANNED_EMBEDDING_DIMENSIONS)
    with pytest.raises(ValueError):
        validate_vectors(np.zeros((1, PLANNED_EMBEDDING_DIMENSIONS), dtype=np.float32))


def test_fake_embedder_prefixes_and_dims():
    emb = FakeEmbedder(EmbedderConfig(revision="fake"))
    passages = emb.embed_passages(["hello"])
    queries = emb.embed_queries(["hello"])
    assert passages.vectors.shape == (1, PLANNED_EMBEDDING_DIMENSIONS)
    assert queries.vectors.shape == (1, PLANNED_EMBEDDING_DIMENSIONS)
    # Different prefixes => different vectors
    assert not np.allclose(passages.vectors, queries.vectors)
    assert E5_PASSAGE_PREFIX.startswith("passage:")
    assert E5_QUERY_PREFIX.startswith("query:")


def test_embed_pipeline_idempotent(db_session: Session):
    chunk = _seed_chunk(db_session, code="GB")
    fake = FakeEmbedder()
    first = run_embedding(db_session, embedder=fake, batch_size=8, force=False)
    assert first.embedded == 1
    count = db_session.scalar(select(func.count()).select_from(DocumentChunkEmbedding))
    assert count == 1
    row = db_session.scalars(select(DocumentChunkEmbedding)).first()
    assert row is not None
    assert row.chunk_id == chunk.id
    assert row.embedding_model_id == PLANNED_EMBEDDING_MODEL_ID
    assert len(row.embedding) == PLANNED_EMBEDDING_DIMENSIONS

    second = run_embedding(db_session, embedder=fake, batch_size=8, force=False)
    assert second.eligible_chunks == 0
    assert second.embedded == 0
    count2 = db_session.scalar(select(func.count()).select_from(DocumentChunkEmbedding))
    assert count2 == 1


def test_embed_pipeline_force_updates(db_session: Session):
    _seed_chunk(db_session, code="NL")
    fake = FakeEmbedder()
    run_embedding(db_session, embedder=fake, force=False)
    before = db_session.scalars(select(DocumentChunkEmbedding)).first()
    assert before is not None
    before_vec = list(before.embedding)
    run_embedding(db_session, embedder=fake, force=True)
    after = db_session.scalars(select(DocumentChunkEmbedding)).first()
    assert after is not None
    # Same fake seed => same vector; row still unique
    assert list(after.embedding) == before_vec
    assert db_session.scalar(select(func.count()).select_from(DocumentChunkEmbedding)) == 1


def test_eu_chunks_remain_distinct(db_session: Session):
    _seed_chunk(db_session, code="EU", text="Directive conditions for highly qualified employment.")
    fake = FakeEmbedder()
    run_embedding(db_session, embedder=fake)
    codes = set(
        db_session.scalars(
            select(DocumentChunk.jurisdiction_code).join(
                DocumentChunkEmbedding,
                DocumentChunkEmbedding.chunk_id == DocumentChunk.id,
            )
        ).all()
    )
    assert codes == {"EU"}


def test_non_current_chunks_not_embedded(db_session: Session):
    chunk = _seed_chunk(db_session, code="IE")
    chunk.is_current_version = False
    db_session.flush()
    fake = FakeEmbedder()
    stats = run_embedding(db_session, embedder=fake)
    assert stats.eligible_chunks == 0
    assert db_session.scalar(select(func.count()).select_from(DocumentChunkEmbedding)) == 0


def test_batching_and_interruption_recovery(db_session: Session):
    c1 = _seed_chunk(db_session, code="AU", text="Australian skilled migration points test overview.")
    c2 = _seed_chunk(
        db_session,
        code="CA",
        text="Canadian Express Entry requires language and education credentials.",
    )
    c3 = _seed_chunk(
        db_session,
        code="DE",
        text="Die Blaue Karte EU gilt für hochqualifizierte Beschäftigung.",
    )

    # Partial run (simulates interrupt after first batch commits).
    first = run_embedding(
        db_session,
        embedder=FakeEmbedder(),
        batch_size=1,
        chunk_ids=[c1.id, c2.id],
    )
    assert first.embedded == 2
    assert db_session.scalar(select(func.count()).select_from(DocumentChunkEmbedding)) == 2

    # Resume without force: only the remaining current chunk is eligible.
    resume = run_embedding(db_session, embedder=FakeEmbedder(), batch_size=2)
    assert resume.eligible_chunks == 1
    assert resume.embedded == 1
    assert resume.skipped_existing >= 0
    stored = set(db_session.scalars(select(DocumentChunkEmbedding.chunk_id)).all())
    assert stored == {c1.id, c2.id, c3.id}


def test_embedder_config_pins_revision_and_prefixes():
    cfg = EmbedderConfig()
    assert cfg.model_id == PLANNED_EMBEDDING_MODEL_ID
    assert cfg.dimensions == PLANNED_EMBEDDING_DIMENSIONS
    assert cfg.revision == E5_MODEL_REVISION
    assert cfg.passage_prefix == "passage: "
    assert cfg.query_prefix == "query: "
    assert cfg.pooling == "mean_attention_mask"
    assert cfg.normalize == "l2"
