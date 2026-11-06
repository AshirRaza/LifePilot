"""Database layer for LifePilot."""

from lifepilot.db.base import Base
from lifepilot.db.models import (
    Authority,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    DocumentContentComparison,
    DocumentExtraction,
    DocumentJurisdiction,
    DocumentVersion,
    EmbeddingModel,
    Jurisdiction,
    Source,
)
from lifepilot.db.session import get_engine, get_session_factory

__all__ = [
    "Authority",
    "Base",
    "Document",
    "DocumentChunk",
    "DocumentChunkEmbedding",
    "DocumentContentComparison",
    "DocumentExtraction",
    "DocumentJurisdiction",
    "DocumentVersion",
    "EmbeddingModel",
    "Jurisdiction",
    "Source",
    "get_engine",
    "get_session_factory",
]
