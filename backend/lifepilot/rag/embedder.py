"""Local multilingual-e5-small embedding (CPU default, optional CUDA).

Document chunks use the ``passage:`` prefix. Callers preparing future queries
must use ``query:`` via :meth:`E5Embedder.embed_queries`.

No paid APIs; inference stays on the local machine.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol, Sequence

import numpy as np

from lifepilot.db.models import (
    PLANNED_EMBEDDING_DIMENSIONS,
    PLANNED_EMBEDDING_MODEL_ID,
)

logger = logging.getLogger(__name__)

E5_MODEL_ID = PLANNED_EMBEDDING_MODEL_ID
# Pinned Hugging Face commit for intfloat/multilingual-e5-small.
E5_MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
E5_DIMENSIONS = PLANNED_EMBEDDING_DIMENSIONS
E5_MAX_LENGTH = 512
E5_PASSAGE_PREFIX = "passage: "
E5_QUERY_PREFIX = "query: "
E5_POOLING = "mean_attention_mask"
E5_NORMALIZE = "l2"


@dataclass(frozen=True)
class EmbedderConfig:
    model_id: str = E5_MODEL_ID
    revision: str = E5_MODEL_REVISION
    dimensions: int = E5_DIMENSIONS
    max_length: int = E5_MAX_LENGTH
    passage_prefix: str = E5_PASSAGE_PREFIX
    query_prefix: str = E5_QUERY_PREFIX
    pooling: str = E5_POOLING
    normalize: str = E5_NORMALIZE
    device: str = "cpu"  # "cpu" | "cuda" | "cuda:0"


@dataclass(frozen=True)
class EmbeddingBatchResult:
    vectors: np.ndarray  # (n, dims) float32
    truncated: list[bool]
    token_counts: list[int]


class Embedder(Protocol):
    config: EmbedderConfig

    def embed_passages(self, texts: Sequence[str]) -> EmbeddingBatchResult: ...

    def embed_queries(self, texts: Sequence[str]) -> EmbeddingBatchResult: ...


def resolve_device(prefer_cuda: bool = False) -> str:
    if not prefer_cuda:
        return "cpu"
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    return "cpu"


def mean_pool(last_hidden_state, attention_mask):
    import torch

    mask = attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).float()
    summed = torch.sum(last_hidden_state * mask, dim=1)
    counts = torch.clamp(mask.sum(dim=1), min=1e-9)
    return summed / counts


def l2_normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-12)
    return (vectors / norms).astype(np.float32)


def validate_vectors(vectors: np.ndarray, *, dimensions: int = E5_DIMENSIONS) -> None:
    if vectors.ndim != 2:
        raise ValueError(f"expected 2-D embedding matrix, got shape {vectors.shape}")
    if vectors.shape[1] != dimensions:
        raise ValueError(f"expected dim={dimensions}, got {vectors.shape[1]}")
    if not np.isfinite(vectors).all():
        raise ValueError("embedding contains non-finite values")
    norms = np.linalg.norm(vectors, axis=1)
    if np.any(norms < 1e-6):
        raise ValueError("embedding contains near-zero vectors")
    if np.any(np.abs(norms - 1.0) > 1e-3):
        raise ValueError("embedding vectors are not L2-normalized")


class E5Embedder:
    """Local E5 embedder using transformers + torch."""

    def __init__(self, config: EmbedderConfig | None = None) -> None:
        self.config = config or EmbedderConfig(device=resolve_device(False))
        self._tokenizer = None
        self._model = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoModel, AutoTokenizer

        logger.info(
            "Loading embedding model id=%s revision=%s device=%s",
            self.config.model_id,
            self.config.revision,
            self.config.device,
        )
        self._tokenizer = AutoTokenizer.from_pretrained(
            self.config.model_id,
            revision=self.config.revision,
        )
        self._model = AutoModel.from_pretrained(
            self.config.model_id,
            revision=self.config.revision,
        )
        self._model.eval()
        self._model.to(self.config.device)
        hidden = int(self._model.config.hidden_size)
        if hidden != self.config.dimensions:
            raise RuntimeError(
                f"Model hidden_size={hidden} does not match expected "
                f"dimensions={self.config.dimensions}"
            )

    def embed_passages(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        prefixed = [f"{self.config.passage_prefix}{t}" for t in texts]
        return self._embed(prefixed)

    def embed_queries(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        prefixed = [f"{self.config.query_prefix}{t}" for t in texts]
        return self._embed(prefixed)

    def _embed(self, prefixed_texts: Sequence[str]) -> EmbeddingBatchResult:
        import torch

        self._ensure_loaded()
        assert self._tokenizer is not None and self._model is not None

        encoded = self._tokenizer(
            list(prefixed_texts),
            padding=True,
            truncation=True,
            max_length=self.config.max_length,
            return_tensors="pt",
        )
        truncated: list[bool] = []
        token_counts: list[int] = []
        # Count tokens without triggering the HF "sequence longer than max" warning.
        prev_max = getattr(self._tokenizer, "model_max_length", self.config.max_length)
        self._tokenizer.model_max_length = int(1e9)
        try:
            for text in prefixed_texts:
                ids = self._tokenizer.encode(text, add_special_tokens=True)
                n = len(ids)
                token_counts.append(min(n, self.config.max_length))
                truncated.append(n > self.config.max_length)
        finally:
            self._tokenizer.model_max_length = prev_max

        input_ids = encoded["input_ids"].to(self.config.device)
        attention_mask = encoded["attention_mask"].to(self.config.device)
        with torch.no_grad():
            outputs = self._model(input_ids=input_ids, attention_mask=attention_mask)
            pooled = mean_pool(outputs.last_hidden_state, attention_mask)
            pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
        vectors = pooled.detach().cpu().numpy().astype(np.float32)
        validate_vectors(vectors, dimensions=self.config.dimensions)
        return EmbeddingBatchResult(
            vectors=vectors,
            truncated=truncated,
            token_counts=token_counts,
        )


class FakeEmbedder:
    """Deterministic embedder for offline tests (no model download)."""

    def __init__(self, config: EmbedderConfig | None = None) -> None:
        self.config = config or EmbedderConfig(revision="fake-test-revision")

    def embed_passages(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        return self._embed(texts, self.config.passage_prefix)

    def embed_queries(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        return self._embed(texts, self.config.query_prefix)

    def _embed(self, texts: Sequence[str], prefix: str) -> EmbeddingBatchResult:
        vectors = []
        truncated = []
        token_counts = []
        for text in texts:
            approx_tokens = (len(prefix) + len(text)) // 4 + 2
            truncated.append(approx_tokens > self.config.max_length)
            token_counts.append(min(approx_tokens, self.config.max_length))
            digest = hashlib.sha256((prefix + text).encode("utf-8")).digest()
            seed = int.from_bytes(digest[:8], "little")
            rng = np.random.default_rng(seed)
            vec = rng.normal(size=self.config.dimensions).astype(np.float32)
            vectors.append(vec)
        arr = l2_normalize(np.stack(vectors, axis=0))
        validate_vectors(arr, dimensions=self.config.dimensions)
        return EmbeddingBatchResult(
            vectors=arr, truncated=truncated, token_counts=token_counts
        )
