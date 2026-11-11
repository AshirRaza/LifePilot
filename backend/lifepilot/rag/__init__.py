"""RAG preparation and internal retrieval (no answer generation)."""

from lifepilot.rag.chunker import ChunkDraft, chunk_structured_document
from lifepilot.rag.embed_pipeline import EmbedRunStats, run_embedding
from lifepilot.rag.embedder import (
    E5_MODEL_ID,
    E5_MODEL_REVISION,
    EmbedderConfig,
    E5Embedder,
    FakeEmbedder,
    get_shared_embedder,
    reset_shared_embedder,
    set_shared_embedder,
)
from lifepilot.rag.pipeline import ChunkRunStats, run_chunking
from lifepilot.rag.retrieval import (
    InternalKnowledgeRetriever,
    InternalQuery,
    RetrievedPassage,
    RetrievalResult,
    RetrievalStatus,
    diversify_by_document,
    reciprocal_rank_fusion,
    select_document_aware_passages,
)

__all__ = [
    "ChunkDraft",
    "ChunkRunStats",
    "E5_MODEL_ID",
    "E5_MODEL_REVISION",
    "E5Embedder",
    "EmbedRunStats",
    "EmbedderConfig",
    "FakeEmbedder",
    "InternalKnowledgeRetriever",
    "InternalQuery",
    "RetrievedPassage",
    "RetrievalResult",
    "RetrievalStatus",
    "chunk_structured_document",
    "diversify_by_document",
    "get_shared_embedder",
    "reciprocal_rank_fusion",
    "reset_shared_embedder",
    "run_chunking",
    "run_embedding",
    "select_document_aware_passages",
    "set_shared_embedder",
]
