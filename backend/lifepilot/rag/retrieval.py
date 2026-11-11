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
from lifepilot.rag.embedder import Embedder

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
    diversify_documents: bool = True
    # Allow a few strong passages per document after diversity-first selection.
    max_chunks_per_document: int = 3
    # Two-stage within-document reselection using existing embeddings/scores.
    document_aware_passages: bool = True
    expand_adjacent_chunks: bool = True


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
    weights: Sequence[float] | None = None,
) -> list[tuple[int, float]]:
    """RRF over ranked id lists. Empty lists are ignored. Ties broken by ascending id.

    Optional per-list weights scale each list's contribution (default 1.0).
    """
    scores: dict[int, float] = {}
    for list_idx, ranked in enumerate(ranked_lists):
        if not ranked:
            continue
        weight = 1.0
        if weights is not None and list_idx < len(weights):
            weight = float(weights[list_idx])
        if weight <= 0:
            continue
        for rank, item_id in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def diversify_by_document(
    ranked_chunk_ids: Sequence[int],
    chunk_to_document: dict[int, int],
    *,
    top_k: int,
    max_chunks_per_document: int = 1,
) -> list[int]:
    """Prefer the highest-ranked chunk per document, then fill remaining slots.

    Pass 1 keeps up to ``max_chunks_per_document`` chunks from each document in
    rank order. Pass 2 adds further chunks (including additional ones from
    already-selected documents) only after distinct documents have been
    considered, so multi-part answers are still possible when top_k is large.
    """
    if top_k <= 0 or not ranked_chunk_ids:
        return []

    selected: list[int] = []
    selected_set: set[int] = set()
    per_doc: dict[int, int] = {}

    for cid in ranked_chunk_ids:
        doc_id = chunk_to_document.get(cid)
        if doc_id is None:
            continue
        count = per_doc.get(doc_id, 0)
        if count >= max_chunks_per_document:
            continue
        selected.append(cid)
        selected_set.add(cid)
        per_doc[doc_id] = count + 1
        if len(selected) >= top_k:
            return selected

    if len(selected) >= top_k:
        return selected

    for cid in ranked_chunk_ids:
        if cid in selected_set:
            continue
        if cid not in chunk_to_document:
            continue
        selected.append(cid)
        selected_set.add(cid)
        if len(selected) >= top_k:
            break
    return selected


def _heading_section(heading_path: str | None) -> str:
    """Normalize heading path to a section key for adjacent compatibility."""
    if not heading_path:
        return ""
    parts = [p.strip() for p in heading_path.split(">") if p.strip()]
    if not parts:
        return ""
    # Compare on parent section when nested; keep leaf if single-level.
    if len(parts) >= 2:
        return " > ".join(parts[:-1]).lower()
    return parts[0].lower()


def select_document_aware_passages(
    document_ranks: Sequence[int],
    per_document_chunks: dict[int, Sequence[tuple[int, float]]],
    chunk_meta: dict[int, tuple[int, str | None]],
    *,
    top_k: int,
    max_chunks_per_document: int = 2,
    expand_adjacent: bool = True,
    adjacent_score_floor: float = 0.92,
) -> list[int]:
    """Select passages after documents have been ranked.

    Diversity-first:
    1. Take the strongest chunk from each document (preserves doc recall@k).
    2. Fill remaining slots with additional strong within-doc passages and
       optional heading-compatible adjacent neighbors.
    """
    if top_k <= 0 or not document_ranks:
        return []

    selected: list[int] = []
    selected_set: set[int] = set()
    per_doc_count: dict[int, int] = {}
    sorted_per_doc: dict[int, list[tuple[int, float]]] = {}
    index_by_doc: dict[int, dict[int, int]] = {}
    score_lookup: dict[int, float] = {}

    for doc_id in document_ranks:
        scored = list(per_document_chunks.get(doc_id, ()))
        if not scored:
            continue
        scored.sort(key=lambda item: (-item[1], item[0]))
        sorted_per_doc[doc_id] = scored
        by_index: dict[int, int] = {}
        for cid, score in scored:
            score_lookup[cid] = score
            meta = chunk_meta.get(cid)
            if meta is not None:
                by_index[meta[0]] = cid
        index_by_doc[doc_id] = by_index

    def _try_add(doc_id: int, cid: int, *, limit: int) -> bool:
        if cid in selected_set:
            return False
        if per_doc_count.get(doc_id, 0) >= limit:
            return False
        selected.append(cid)
        selected_set.add(cid)
        per_doc_count[doc_id] = per_doc_count.get(doc_id, 0) + 1
        return True

    available_docs = [d for d in document_ranks if d in sorted_per_doc]
    boost_n = min(3, len(available_docs)) if max_chunks_per_document > 1 else 0
    boost_docs = available_docs[:boost_n]
    rest_docs = available_docs[boost_n:]
    adjacent_docs = boost_docs or available_docs[:1]

    # Interleave depth-0 picks across boost docs first (document diversity at top).
    for doc_id in boost_docs:
        scored = sorted_per_doc[doc_id]
        if not scored:
            continue
        _try_add(doc_id, scored[0][0], limit=max_chunks_per_document)
        if len(selected) >= top_k:
            return selected

    # Round-robin deeper passages for boost docs so each gets a 2nd chunk
    # before any receives a 3rd (covers within-doc ranks 2..max).
    if max_chunks_per_document > 1 and boost_docs:
        depth = 1
        while len(selected) < top_k and depth < max_chunks_per_document:
            progressed = False
            for doc_id in boost_docs:
                scored = sorted_per_doc.get(doc_id, [])
                if depth >= len(scored):
                    continue
                if _try_add(
                    doc_id, scored[depth][0], limit=max_chunks_per_document
                ):
                    progressed = True
                    if len(selected) >= top_k:
                        return selected
            if not progressed:
                break
            depth += 1

    # Fill remaining slots with other documents (one best chunk each).
    for doc_id in rest_docs:
        if len(selected) >= top_k:
            break
        scored = sorted_per_doc.get(doc_id, [])
        if not scored:
            continue
        _try_add(doc_id, scored[0][0], limit=1)

    # Optional adjacent same-section neighbors for boosted / primary docs.
    adjacent_limit = max_chunks_per_document + (1 if expand_adjacent else 0)
    if expand_adjacent and len(selected) < top_k:
        for doc_id in adjacent_docs:
            if len(selected) >= top_k:
                break
            seeded = [
                cid
                for cid, _score in sorted_per_doc.get(doc_id, [])
                if cid in selected_set
            ]
            if not seeded:
                continue
            best_score = max(score_lookup.get(cid, 0.0) for cid in seeded)
            floor = best_score * adjacent_score_floor if best_score > 0 else 0.0
            for cid in list(seeded):
                if len(selected) >= top_k:
                    break
                meta = chunk_meta.get(cid)
                if meta is None:
                    continue
                chunk_index, heading = meta
                section = _heading_section(heading)
                for neighbor_index in (chunk_index - 1, chunk_index + 1):
                    neighbor_id = index_by_doc.get(doc_id, {}).get(neighbor_index)
                    if neighbor_id is None:
                        continue
                    neighbor_meta = chunk_meta.get(neighbor_id)
                    if neighbor_meta is None:
                        continue
                    neighbor_score = score_lookup.get(neighbor_id, 0.0)
                    if neighbor_score < floor:
                        continue
                    neighbor_section = _heading_section(neighbor_meta[1])
                    if section and neighbor_section and section != neighbor_section:
                        continue
                    if (
                        _try_add(doc_id, neighbor_id, limit=adjacent_limit)
                        and len(selected) >= top_k
                    ):
                        return selected
    return selected

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
            # Reuse process-wide weights; constructing E5Embedder per request
            # reloads the model from disk and adds multi-second latency.
            from lifepilot.rag.embedder import get_shared_embedder

            self.embedder = get_shared_embedder(prefer_cuda=prefer_cuda)
        # Semantic weight > keyword so weak lexical hits cannot freely displace
        # strong semantic matches under hybrid RRF (Phase 8F NL HSM failure).
        self.hybrid_semantic_weight = 2.0
        self.hybrid_keyword_weight = 1.0

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

        # Oversample candidates so document-aware selection still yields top_k.
        candidate_limit = max(top_k * 8, 50)
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
            ranked_ids = [cid for cid, _ in semantic_hits]
            score_map = {cid: score for cid, score in semantic_hits}
            sem_rank = {cid: i for i, (cid, _) in enumerate(semantic_hits, start=1)}
            kw_rank: dict[int, int] = {}
            kw_score: dict[int, float] = {}
            sem_score = score_map
        elif mode == "keyword":
            ranked_ids = [cid for cid, _ in keyword_hits]
            score_map = {cid: score for cid, score in keyword_hits}
            sem_rank = {}
            kw_rank = {cid: i for i, (cid, _) in enumerate(keyword_hits, start=1)}
            kw_score = score_map
            sem_score = {}
        else:
            ranked_ids, score_map = self._fuse_hybrid(semantic_hits, keyword_hits)
            sem_rank = {cid: i for i, (cid, _) in enumerate(semantic_hits, start=1)}
            kw_rank = {cid: i for i, (cid, _) in enumerate(keyword_hits, start=1)}
            sem_score = {cid: s for cid, s in semantic_hits}
            kw_score = {cid: s for cid, s in keyword_hits}

        max_per_doc = max(1, int(query.max_chunks_per_document))
        if query.diversify_documents and query.document_aware_passages:
            ordered_ids, score_map, extra_sem, extra_kw = self._document_aware_select(
                qtext,
                jurisdiction,
                mode=mode,
                ranked_ids=ranked_ids,
                score_map=score_map,
                top_k=top_k,
                max_chunks_per_document=max_per_doc,
                expand_adjacent=bool(query.expand_adjacent_chunks),
            )
            sem_score = {**sem_score, **extra_sem}
            kw_score = {**kw_score, **extra_kw}
        elif query.diversify_documents:
            doc_map = self._chunk_document_map(ranked_ids)
            ordered_ids = diversify_by_document(
                ranked_ids,
                doc_map,
                top_k=top_k,
                max_chunks_per_document=max_per_doc,
            )
        else:
            ordered_ids = ranked_ids[:top_k]

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

    def _fuse_hybrid(
        self,
        semantic_hits: list[tuple[int, float]],
        keyword_hits: list[tuple[int, float]],
    ) -> tuple[list[int], dict[int, float]]:
        """Fuse semantic + keyword ranks with empty-list and weak-keyword guards."""
        semantic_ids = [cid for cid, _ in semantic_hits]
        keyword_ids = [cid for cid, _ in keyword_hits]

        # Empty keyword → semantic-only (do not invent lexical signal).
        if not keyword_ids:
            score_map = {cid: score for cid, score in semantic_hits}
            return semantic_ids, score_map

        # Empty semantic → keyword-only.
        if not semantic_ids:
            score_map = {cid: score for cid, score in keyword_hits}
            return keyword_ids, score_map

        # Drop weak keyword-only candidates that never appear in the semantic
        # pool and sit far below the strongest keyword hit. This prevents low
        # lexical noise from flooding RRF and displacing strong semantic matches.
        filtered_keyword_ids = self._filter_weak_keyword_ids(
            semantic_ids=semantic_ids,
            keyword_hits=keyword_hits,
        )

        fused = reciprocal_rank_fusion(
            [semantic_ids, filtered_keyword_ids],
            k=self.rrf_k,
            weights=[self.hybrid_semantic_weight, self.hybrid_keyword_weight],
        )
        ordered = [cid for cid, _ in fused]
        score_map = {cid: score for cid, score in fused}
        return ordered, score_map

    def _filter_weak_keyword_ids(
        self,
        *,
        semantic_ids: Sequence[int],
        keyword_hits: Sequence[tuple[int, float]],
    ) -> list[int]:
        """Keep keyword hits that reinforce semantic candidates or are strong.

        A keyword-only id is kept only when its keyword score is at least
        ``relative_floor`` of the best keyword score (default 0.35). Weak
        keyword-only hits are discarded so they cannot displace semantic gold.
        """
        if not keyword_hits:
            return []
        semantic_set = set(semantic_ids)
        best_kw = max(score for _, score in keyword_hits) if keyword_hits else 0.0
        relative_floor = 0.35
        kept: list[int] = []
        for cid, score in keyword_hits:
            if cid in semantic_set:
                kept.append(cid)
                continue
            if best_kw <= 0:
                continue
            if score >= best_kw * relative_floor:
                kept.append(cid)
        return kept

    def _chunk_document_map(self, chunk_ids: Sequence[int]) -> dict[int, int]:
        if not chunk_ids:
            return {}
        rows = self.session.execute(
            select(DocumentChunk.id, DocumentChunk.document_id).where(
                DocumentChunk.id.in_(list(chunk_ids))
            )
        ).all()
        return {int(cid): int(doc_id) for cid, doc_id in rows}

    def _document_aware_select(
        self,
        query: str,
        jurisdiction: str,
        *,
        mode: RetrievalMode,
        ranked_ids: Sequence[int],
        score_map: dict[int, float],
        top_k: int,
        max_chunks_per_document: int,
        expand_adjacent: bool,
    ) -> tuple[list[int], dict[int, float], dict[int, float], dict[int, float]]:
        """Two-stage selection: rank docs from the pool, then reselect passages.

        Returns ordered chunk ids, merged score map, and any extra semantic /
        keyword scores discovered during within-document search.
        """
        if not ranked_ids:
            return [], score_map, {}, {}

        doc_map = self._chunk_document_map(ranked_ids)
        doc_best: dict[int, float] = {}
        doc_first_rank: dict[int, int] = {}
        for rank, cid in enumerate(ranked_ids, start=1):
            doc_id = doc_map.get(cid)
            if doc_id is None:
                continue
            score = float(score_map.get(cid, 0.0))
            if doc_id not in doc_best or score > doc_best[doc_id]:
                doc_best[doc_id] = score
            doc_first_rank.setdefault(doc_id, rank)

        # Prefer stronger document scores; stable tie-break by first appearance.
        document_ranks = sorted(
            doc_best.keys(),
            key=lambda d: (-doc_best[d], doc_first_rank.get(d, 10**9), d),
        )
        # Bound within-doc work to the documents that can appear in top_k.
        doc_budget = min(len(document_ranks), top_k)
        document_ranks = document_ranks[: max(doc_budget, 1)]

        within_limit = max(10, max_chunks_per_document * 4)
        per_document_chunks: dict[int, list[tuple[int, float]]] = {}
        merged_scores = dict(score_map)
        extra_sem: dict[int, float] = {}
        extra_kw: dict[int, float] = {}
        all_candidate_ids: set[int] = set()

        query_vec_literal: str | None = None
        if mode in ("semantic", "hybrid"):
            vector = self.embedder.embed_queries([query]).vectors[0]
            query_vec_literal = (
                "[" + ",".join(f"{float(x):.8f}" for x in vector.tolist()) + "]"
            )

        for doc_id in document_ranks:
            within_sem: list[tuple[int, float]] = []
            within_kw: list[tuple[int, float]] = []
            # Passage selection inside a document prefers semantic similarity even
            # for hybrid mode; keyword remains available for keyword-only mode and
            # for provenance scores. Hybrid already chose the document via RRF.
            if mode in ("semantic", "hybrid") and query_vec_literal is not None:
                within_sem = self._semantic_search_in_document(
                    jurisdiction,
                    document_id=doc_id,
                    limit=within_limit,
                    query_vec_literal=query_vec_literal,
                )
                for cid, score in within_sem:
                    extra_sem[cid] = score
            if mode == "keyword":
                within_kw = self._keyword_search_in_document(
                    query, jurisdiction, document_id=doc_id, limit=within_limit
                )
                for cid, score in within_kw:
                    extra_kw[cid] = score

            if mode == "keyword":
                scored_map = {cid: score for cid, score in within_kw}
            else:
                scored_map = {cid: score for cid, score in within_sem}

            # Append pool-only chunks missing from the within-doc window so a
            # shallow limit cannot drop a jurisdiction-pool hit entirely.
            # Within-doc scores remain authoritative for ranking.
            for cid in ranked_ids:
                if doc_map.get(cid) != doc_id or cid in scored_map:
                    continue
                scored_map[cid] = float(score_map.get(cid, 0.0))

            scored = sorted(
                scored_map.items(), key=lambda item: (-item[1], item[0])
            )
            if not scored:
                continue

            per_document_chunks[doc_id] = scored
            for cid, score in scored:
                merged_scores[cid] = float(score)
                all_candidate_ids.add(cid)

        # Adjacent expansion only considers neighbors already scored within-doc
        # (same current version / document boundary preserved by the SQL filters).
        chunk_meta = self._chunk_index_heading_map(all_candidate_ids)
        ordered = select_document_aware_passages(
            document_ranks,
            per_document_chunks,
            chunk_meta,
            top_k=top_k,
            max_chunks_per_document=max_chunks_per_document,
            expand_adjacent=expand_adjacent,
        )
        return ordered, merged_scores, extra_sem, extra_kw

    def _chunk_index_heading_map(
        self, chunk_ids: Sequence[int] | set[int]
    ) -> dict[int, tuple[int, str | None]]:
        ids = list(chunk_ids)
        if not ids:
            return {}
        rows = self.session.execute(
            select(
                DocumentChunk.id,
                DocumentChunk.chunk_index,
                DocumentChunk.heading_path,
            ).where(DocumentChunk.id.in_(ids))
        ).all()
        return {
            int(cid): (int(idx), heading)
            for cid, idx, heading in rows
        }

    def _semantic_search_in_document(
        self,
        jurisdiction: str,
        *,
        document_id: int,
        limit: int,
        query_vec_literal: str,
    ) -> list[tuple[int, float]]:
        sql = text(
            """
            SELECT c.id AS chunk_id,
                   1 - (e.embedding <=> CAST(:qvec AS vector)) AS score
            FROM document_chunk_embeddings e
            JOIN document_chunks c ON c.id = e.chunk_id
            WHERE e.embedding_model_id = :model_id
              AND c.is_current_version IS TRUE
              AND c.jurisdiction_code = :jurisdiction
              AND c.document_id = :document_id
            ORDER BY e.embedding <=> CAST(:qvec AS vector) ASC, c.id ASC
            LIMIT :lim
            """
        )
        rows = self.session.execute(
            sql,
            {
                "qvec": query_vec_literal,
                "model_id": self.embedding_model_id,
                "jurisdiction": jurisdiction,
                "document_id": document_id,
                "lim": limit,
            },
        ).all()
        return [(int(r[0]), float(r[1])) for r in rows]

    def _keyword_search_in_document(
        self,
        query: str,
        jurisdiction: str,
        *,
        document_id: int,
        limit: int,
    ) -> list[tuple[int, float]]:
        sql = text(
            """
            SELECT c.id AS chunk_id,
                   ts_rank_cd(c.search_tsv, plainto_tsquery('english', :q)) AS score
            FROM document_chunks c
            WHERE c.is_current_version IS TRUE
              AND c.jurisdiction_code = :jurisdiction
              AND c.document_id = :document_id
              AND c.search_tsv @@ plainto_tsquery('english', :q)
            ORDER BY score DESC, c.id ASC
            LIMIT :lim
            """
        )
        rows = self.session.execute(
            sql,
            {
                "q": query,
                "jurisdiction": jurisdiction,
                "document_id": document_id,
                "lim": limit,
            },
        ).all()
        return [(int(r[0]), float(r[1])) for r in rows]

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
