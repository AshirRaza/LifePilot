"""Internal hybrid retrieval over document_chunks (no answer generation)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lifepilot.db.models import (
    PLANNED_EMBEDDING_MODEL_ID,
    Document,
    DocumentChunk,
)
from lifepilot.rag.embedder import E5Embedder, Embedder, EmbedderConfig, resolve_device

logger = logging.getLogger(__name__)

VerificationState = Literal["unverified_retrieval"]
RetrievalMode = Literal["semantic", "keyword", "hybrid"]


class RetrievalStatus(str, Enum):
    OK = "OK"
    NO_RESULTS = "NO_RESULTS"
    UNSUPPORTED_FILTER = "UNSUPPORTED_FILTER"
    INVALID_REQUEST = "INVALID_REQUEST"


@dataclass(frozen=True)
class InternalQuery:
    query: str
    jurisdiction: str
    mode: RetrievalMode = "hybrid"
    top_k: int = 8
    pathway_id: str | None = None
    requirement: str | None = None


@dataclass
class RetrievedPassage:
    chunk_id: int
    document_id: int
    document_version_id: int
    source_id: int
    jurisdiction_code: str
    official_url: str
    heading_path: str | None
    content_text: str
    score: float
    semantic_rank: int | None = None
    keyword_rank: int | None = None
    semantic_score: float | None = None
    keyword_score: float | None = None
    pathway_id: str | None = None
    requirement_category: str | None = None
    verification_state: VerificationState = "unverified_retrieval"
    applicability_warning: str = (
        "Retrieved passage is not validated pathway evidence and must not be "
        "treated as COMPLETE coverage."
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "source_id": self.source_id,
            "jurisdiction_code": self.jurisdiction_code,
            "official_url": self.official_url,
            "heading_path": self.heading_path,
            "content_text": self.content_text,
            "score": round(self.score, 6),
            "semantic_rank": self.semantic_rank,
            "keyword_rank": self.keyword_rank,
            "semantic_score": (
                None if self.semantic_score is None else round(self.semantic_score, 6)
            ),
            "keyword_score": (
                None if self.keyword_score is None else round(self.keyword_score, 6)
            ),
            "pathway_id": self.pathway_id,
            "requirement_category": self.requirement_category,
            "verification_state": self.verification_state,
            "applicability_warning": self.applicability_warning,
        }


@dataclass
class RetrievalResult:
    status: RetrievalStatus
    query: str
    jurisdiction: str
    mode: RetrievalMode
    passages: list[RetrievedPassage] = field(default_factory=list)
    message: str | None = None
    latency_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "query": self.query,
            "jurisdiction": self.jurisdiction,
            "mode": self.mode,
            "message": self.message,
            "latency_ms": round(self.latency_ms, 2),
            "warnings": self.warnings,
            "passages": [p.as_dict() for p in self.passages],
        }


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[int]],
    *,
    k: int = 60,
) -> list[tuple[int, float]]:
    """RRF over ranked id lists. Ties broken by ascending id."""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def _normalize_jurisdiction(code: str) -> str:
    return (code or "").strip().upper()


class InternalKnowledgeRetriever:
    """Jurisdiction-scoped semantic / keyword / hybrid chunk retrieval."""

    def __init__(
        self,
        session: Session,
        *,
        embedder: Embedder | None = None,
        prefer_cuda: bool = False,
        embedding_model_id: str = PLANNED_EMBEDDING_MODEL_ID,
        rrf_k: int = 60,
    ) -> None:
        self.session = session
        self.embedding_model_id = embedding_model_id
        self.rrf_k = rrf_k
        if embedder is not None:
            self.embedder = embedder
        else:
            device = resolve_device(prefer_cuda)
            self.embedder = E5Embedder(EmbedderConfig(device=device))

    def retrieve(self, query: InternalQuery) -> RetrievalResult:
        started = time.perf_counter()
        jurisdiction = _normalize_jurisdiction(query.jurisdiction)
        qtext = (query.query or "").strip()
        top_k = max(1, min(int(query.top_k), 50))
        mode: RetrievalMode = query.mode

        if not jurisdiction:
            return RetrievalResult(
                status=RetrievalStatus.INVALID_REQUEST,
                query=qtext,
                jurisdiction=jurisdiction,
                mode=mode,
                message="jurisdiction is required and must not be empty",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        if not qtext:
            return RetrievalResult(
                status=RetrievalStatus.INVALID_REQUEST,
                query=qtext,
                jurisdiction=jurisdiction,
                mode=mode,
                message="query text is required",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        if mode not in ("semantic", "keyword", "hybrid"):
            return RetrievalResult(
                status=RetrievalStatus.INVALID_REQUEST,
                query=qtext,
                jurisdiction=jurisdiction,
                mode=mode,
                message=f"unsupported mode: {mode}",
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        warnings = [
            "verification_state=unverified_retrieval",
            "Passages are jurisdiction-scoped retrieval hits, not validated "
            "pathway evidence classifications.",
        ]

        # Pathway / requirement filters are unsupported until curated associations exist.
        if query.pathway_id or query.requirement:
            associated = self._count_pathway_tagged(jurisdiction, query.pathway_id)
            if associated == 0:
                return RetrievalResult(
                    status=RetrievalStatus.UNSUPPORTED_FILTER,
                    query=qtext,
                    jurisdiction=jurisdiction,
                    mode=mode,
                    message=(
                        "No validated pathway/requirement associations exist on "
                        "current chunks; refusing to invent filters from similarity."
                    ),
                    latency_ms=(time.perf_counter() - started) * 1000,
                    warnings=warnings,
                )

        candidate_limit = max(top_k * 5, 40)
        semantic_hits: list[tuple[int, float]] = []
        keyword_hits: list[tuple[int, float]] = []

        if mode in ("semantic", "hybrid"):
            semantic_hits = self._semantic_search(
                qtext, jurisdiction, limit=candidate_limit
            )
        if mode in ("keyword", "hybrid"):
            keyword_hits = self._keyword_search(
                qtext, jurisdiction, limit=candidate_limit
            )

        if mode == "semantic":
            ordered_ids = [cid for cid, _ in semantic_hits[:top_k]]
            score_map = {cid: score for cid, score in semantic_hits}
            sem_rank = {cid: i for i, (cid, _) in enumerate(semantic_hits, start=1)}
            kw_rank: dict[int, int] = {}
            kw_score: dict[int, float] = {}
            sem_score = score_map
        elif mode == "keyword":
            ordered_ids = [cid for cid, _ in keyword_hits[:top_k]]
            score_map = {cid: score for cid, score in keyword_hits}
            sem_rank = {}
            kw_rank = {cid: i for i, (cid, _) in enumerate(keyword_hits, start=1)}
            kw_score = score_map
            sem_score = {}
        else:
            fused = reciprocal_rank_fusion(
                [
                    [cid for cid, _ in semantic_hits],
                    [cid for cid, _ in keyword_hits],
                ],
                k=self.rrf_k,
            )
            ordered_ids = [cid for cid, _ in fused[:top_k]]
            score_map = {cid: score for cid, score in fused}
            sem_rank = {cid: i for i, (cid, _) in enumerate(semantic_hits, start=1)}
            kw_rank = {cid: i for i, (cid, _) in enumerate(keyword_hits, start=1)}
            sem_score = {cid: s for cid, s in semantic_hits}
            kw_score = {cid: s for cid, s in keyword_hits}

        passages = self._hydrate_passages(
            ordered_ids,
            score_map=score_map,
            sem_rank=sem_rank,
            kw_rank=kw_rank,
            sem_score=sem_score,
            kw_score=kw_score,
            expected_jurisdiction=jurisdiction,
        )

        status = RetrievalStatus.OK if passages else RetrievalStatus.NO_RESULTS
        return RetrievalResult(
            status=status,
            query=qtext,
            jurisdiction=jurisdiction,
            mode=mode,
            passages=passages,
            message=None if passages else "No matching current chunks for jurisdiction",
            latency_ms=(time.perf_counter() - started) * 1000,
            warnings=warnings,
        )

    def _count_pathway_tagged(self, jurisdiction: str, pathway_id: str | None) -> int:
        stmt = select(DocumentChunk.id).where(
            DocumentChunk.is_current_version.is_(True),
            DocumentChunk.jurisdiction_code == jurisdiction,
            DocumentChunk.pathway_id.is_not(None),
        )
        if pathway_id:
            stmt = stmt.where(DocumentChunk.pathway_id == pathway_id)
        stmt = stmt.limit(1)
        return 1 if self.session.scalars(stmt).first() is not None else 0

    def _semantic_search(
        self, query: str, jurisdiction: str, *, limit: int
    ) -> list[tuple[int, float]]:
        vector = self.embedder.embed_queries([query]).vectors[0]
        vec_literal = "[" + ",".join(f"{float(x):.8f}" for x in vector.tolist()) + "]"
        # Cosine distance via pgvector; similarity = 1 - distance for unit vectors.
        sql = text(
            """
            SELECT c.id AS chunk_id,
                   1 - (e.embedding <=> CAST(:qvec AS vector)) AS score
            FROM document_chunk_embeddings e
            JOIN document_chunks c ON c.id = e.chunk_id
            WHERE e.embedding_model_id = :model_id
              AND c.is_current_version IS TRUE
              AND c.jurisdiction_code = :jurisdiction
            ORDER BY e.embedding <=> CAST(:qvec AS vector) ASC, c.id ASC
            LIMIT :lim
            """
        )
        rows = self.session.execute(
            sql,
            {
                "qvec": vec_literal,
                "model_id": self.embedding_model_id,
                "jurisdiction": jurisdiction,
                "lim": limit,
            },
        ).all()
        return [(int(r[0]), float(r[1])) for r in rows]

    def _keyword_search(
        self, query: str, jurisdiction: str, *, limit: int
    ) -> list[tuple[int, float]]:
        sql = text(
            """
            SELECT c.id AS chunk_id,
                   ts_rank_cd(c.search_tsv, plainto_tsquery('english', :q)) AS score
            FROM document_chunks c
            WHERE c.is_current_version IS TRUE
              AND c.jurisdiction_code = :jurisdiction
              AND c.search_tsv @@ plainto_tsquery('english', :q)
            ORDER BY score DESC, c.id ASC
            LIMIT :lim
            """
        )
        rows = self.session.execute(
            sql,
            {"q": query, "jurisdiction": jurisdiction, "lim": limit},
        ).all()
        return [(int(r[0]), float(r[1])) for r in rows]

    def _hydrate_passages(
        self,
        chunk_ids: Sequence[int],
        *,
        score_map: dict[int, float],
        sem_rank: dict[int, int],
        kw_rank: dict[int, int],
        sem_score: dict[int, float],
        kw_score: dict[int, float],
        expected_jurisdiction: str,
    ) -> list[RetrievedPassage]:
        if not chunk_ids:
            return []
        stmt = (
            select(
                DocumentChunk,
                Document.canonical_url,
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.id.in_(list(chunk_ids)))
        )
        rows = self.session.execute(stmt).all()
        by_id: dict[int, tuple[DocumentChunk, str]] = {
            int(chunk.id): (chunk, str(url)) for chunk, url in rows
        }
        passages: list[RetrievedPassage] = []
        for cid in chunk_ids:
            item = by_id.get(cid)
            if item is None:
                continue
            chunk, url = item
            if chunk.jurisdiction_code != expected_jurisdiction:
                continue
            if not chunk.is_current_version:
                continue
            passages.append(
                RetrievedPassage(
                    chunk_id=int(chunk.id),
                    document_id=int(chunk.document_id),
                    document_version_id=int(chunk.document_version_id),
                    source_id=int(chunk.source_id),
                    jurisdiction_code=chunk.jurisdiction_code,
                    official_url=url,
                    heading_path=chunk.heading_path,
                    content_text=chunk.content_text,
                    score=float(score_map.get(cid, 0.0)),
                    semantic_rank=sem_rank.get(cid),
                    keyword_rank=kw_rank.get(cid),
                    semantic_score=sem_score.get(cid),
                    keyword_score=kw_score.get(cid),
                    pathway_id=chunk.pathway_id,
                    requirement_category=chunk.requirement_category,
                )
            )
        return passages
