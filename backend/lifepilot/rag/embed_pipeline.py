"""Batch embedding of current document chunks into pgvector."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from lifepilot.db.models import (
    PLANNED_EMBEDDING_MODEL_ID,
    DocumentChunk,
    DocumentChunkEmbedding,
    EmbeddingModel,
)
from lifepilot.rag.embedder import (
    E5_DIMENSIONS,
    E5_MAX_LENGTH,
    E5_MODEL_ID,
    E5_MODEL_REVISION,
    E5_NORMALIZE,
    E5_POOLING,
    Embedder,
    EmbedderConfig,
    E5Embedder,
    resolve_device,
    validate_vectors,
)

logger = logging.getLogger(__name__)


@dataclass
class EmbedRunStats:
    eligible_chunks: int = 0
    embedded: int = 0
    skipped_existing: int = 0
    failed_batches: int = 0
    truncated_chunks: int = 0
    elapsed_seconds: float = 0.0
    device: str = "cpu"
    model_id: str = E5_MODEL_ID
    model_revision: str = E5_MODEL_REVISION
    failures: list[dict[str, Any]] = field(default_factory=list)
    by_jurisdiction: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible_chunks": self.eligible_chunks,
            "embedded": self.embedded,
            "skipped_existing": self.skipped_existing,
            "failed_batches": self.failed_batches,
            "truncated_chunks": self.truncated_chunks,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "device": self.device,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "pooling": E5_POOLING,
            "normalize": E5_NORMALIZE,
            "dimensions": E5_DIMENSIONS,
            "max_length": E5_MAX_LENGTH,
            "by_jurisdiction": dict(sorted(self.by_jurisdiction.items())),
            "failures": self.failures[:50],
        }


def ensure_embedding_model_row(session: Session, config: EmbedderConfig) -> None:
    notes = (
        f"revision={config.revision}; pooling={config.pooling}; "
        f"normalize={config.normalize}; max_length={config.max_length}; "
        f"passage_prefix={config.passage_prefix!r}; query_prefix={config.query_prefix!r}"
    )
    existing = session.get(EmbeddingModel, config.model_id)
    if existing is None:
        session.add(
            EmbeddingModel(
                id=config.model_id,
                display_name="Multilingual E5 Small",
                dimensions=config.dimensions,
                distance_metric="cosine",
                notes=notes,
            )
        )
    else:
        existing.dimensions = config.dimensions
        existing.notes = notes
        existing.distance_metric = "cosine"
    session.flush()


def iter_eligible_chunk_ids(
    session: Session,
    *,
    model_id: str = PLANNED_EMBEDDING_MODEL_ID,
    chunk_ids: Sequence[int] | None = None,
    jurisdiction_codes: Sequence[str] | None = None,
    limit: int | None = None,
    only_missing: bool = True,
) -> list[int]:
    stmt = (
        select(DocumentChunk.id)
        .where(DocumentChunk.is_current_version.is_(True))
        .order_by(DocumentChunk.id)
    )
    if chunk_ids:
        stmt = stmt.where(DocumentChunk.id.in_(list(chunk_ids)))
    if jurisdiction_codes:
        codes = [c.upper() for c in jurisdiction_codes]
        stmt = stmt.where(DocumentChunk.jurisdiction_code.in_(codes))
    if only_missing:
        existing = select(DocumentChunkEmbedding.chunk_id).where(
            DocumentChunkEmbedding.embedding_model_id == model_id
        )
        stmt = stmt.where(DocumentChunk.id.not_in(existing))
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def _upsert_embeddings(
    session: Session,
    *,
    model_id: str,
    chunk_ids: Sequence[int],
    vectors: Sequence[Sequence[float]],
) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        {
            "chunk_id": int(cid),
            "embedding_model_id": model_id,
            "embedding": list(map(float, vec)),
            "indexed_at": now,
        }
        for cid, vec in zip(chunk_ids, vectors, strict=True)
    ]
    stmt = pg_insert(DocumentChunkEmbedding).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_document_chunk_embeddings_chunk_model",
        set_={
            "embedding": stmt.excluded.embedding,
            "indexed_at": stmt.excluded.indexed_at,
        },
    )
    session.execute(stmt)


def run_embedding(
    session: Session,
    *,
    embedder: Embedder | None = None,
    prefer_cuda: bool = False,
    batch_size: int = 32,
    chunk_ids: Sequence[int] | None = None,
    jurisdiction_codes: Sequence[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> EmbedRunStats:
    """Embed eligible current chunks. Safe to interrupt and rerun."""
    device = resolve_device(prefer_cuda)
    config = EmbedderConfig(device=device)
    embedder = embedder or E5Embedder(config)
    stats = EmbedRunStats(
        device=embedder.config.device,
        model_id=embedder.config.model_id,
        model_revision=embedder.config.revision,
    )
    ensure_embedding_model_row(session, embedder.config)
    session.commit()

    total_current = len(
        list(
            session.scalars(
                select(DocumentChunk.id).where(
                    DocumentChunk.is_current_version.is_(True)
                )
            ).all()
        )
    )
    ids = iter_eligible_chunk_ids(
        session,
        model_id=embedder.config.model_id,
        chunk_ids=chunk_ids,
        jurisdiction_codes=jurisdiction_codes,
        limit=limit,
        only_missing=not force,
    )
    stats.eligible_chunks = len(ids)
    if not force and not chunk_ids and not jurisdiction_codes and limit is None:
        stats.skipped_existing = max(0, total_current - stats.eligible_chunks)
    if progress:
        progress(
            f"Eligible chunks: {stats.eligible_chunks} "
            f"(model={stats.model_id}@{stats.model_revision[:12]} device={stats.device})"
        )

    started = time.perf_counter()
    batch_size = max(1, batch_size)

    for start in range(0, len(ids), batch_size):
        batch_ids = ids[start : start + batch_size]
        rows = session.execute(
            select(
                DocumentChunk.id,
                DocumentChunk.content_text,
                DocumentChunk.jurisdiction_code,
            ).where(DocumentChunk.id.in_(batch_ids))
        ).all()
        # Preserve batch_ids order
        by_id = {int(r[0]): (str(r[1]), str(r[2])) for r in rows}
        ordered_ids: list[int] = []
        texts: list[str] = []
        jurisdictions: list[str] = []
        for cid in batch_ids:
            if cid not in by_id:
                continue
            text_value, jur = by_id[cid]
            ordered_ids.append(cid)
            texts.append(text_value)
            jurisdictions.append(jur)
        if not ordered_ids:
            continue

        try:
            result = embedder.embed_passages(texts)
            validate_vectors(result.vectors, dimensions=embedder.config.dimensions)
            _upsert_embeddings(
                session,
                model_id=embedder.config.model_id,
                chunk_ids=ordered_ids,
                vectors=result.vectors,
            )
            session.commit()
            stats.embedded += len(ordered_ids)
            stats.truncated_chunks += sum(1 for t in result.truncated if t)
            for jur in jurisdictions:
                stats.by_jurisdiction[jur] = stats.by_jurisdiction.get(jur, 0) + 1
        except Exception as exc:  # noqa: BLE001 — isolate batch failures
            session.rollback()
            stats.failed_batches += 1
            stats.failures.append(
                {
                    "chunk_ids": ordered_ids[:10],
                    "batch_size": len(ordered_ids),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            logger.exception("embedding batch failed size=%s", len(ordered_ids))

        if progress and ((start // batch_size) % 5 == 0 or start + batch_size >= len(ids)):
            progress(
                f"Progress {min(start + batch_size, len(ids))}/{len(ids)} "
                f"embedded={stats.embedded} failed_batches={stats.failed_batches} "
                f"truncated={stats.truncated_chunks}"
            )

    stats.elapsed_seconds = time.perf_counter() - started
    return stats
