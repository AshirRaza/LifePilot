"""RAG preparation: cleaning, chunking, and local embeddings (no retrieval)."""

from lifepilot.rag.chunker import ChunkDraft, chunk_structured_document
from lifepilot.rag.embed_pipeline import EmbedRunStats, run_embedding
from lifepilot.rag.embedder import (
    E5_MODEL_ID,
    E5_MODEL_REVISION,
    EmbedderConfig,
    E5Embedder,
    FakeEmbedder,
)
from lifepilot.rag.pipeline import ChunkRunStats, run_chunking

__all__ = [
    "ChunkDraft",
    "ChunkRunStats",
    "E5_MODEL_ID",
    "E5_MODEL_REVISION",
    "E5Embedder",
    "EmbedRunStats",
    "EmbedderConfig",
    "FakeEmbedder",
    "chunk_structured_document",
    "run_chunking",
    "run_embedding",
]
