"""Evidence and citation contracts shared by internal RAG and external APIs."""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvidenceSourceType(str, Enum):
    INTERNAL_RAG = "internal_rag"
    EXTERNAL_API = "external_api"


class EvidenceVerificationState(str, Enum):
    """Distinct verification labels — never conflate retrieval with COMPLETE."""

    UNVERIFIED_RETRIEVAL = "unverified_retrieval"
    UNVERIFIED_EXTERNAL = "unverified_external"


DEFAULT_APPLICABILITY = (
    "Retrieved material is not validated pathway evidence, does not establish "
    "legal eligibility, and must not be treated as COMPLETE or human-verified."
)


class EvidenceItem(BaseModel):
    """Normalized evidence reference suitable for later claim-citation checks."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="Stable id for citation binding (hash of provenance keys).",
    )
    source_type: EvidenceSourceType
    jurisdiction: str = Field(..., min_length=2, max_length=8)
    source_url: str | None = None
    document_id: int | str | None = None
    document_version_id: int | str | None = None
    chunk_id: int | str | None = None
    provider: str | None = None
    heading_path: str | None = None
    passage_text: str = Field(..., min_length=1)
    retrieval_score: float | None = None
    verification_state: EvidenceVerificationState
    provenance: dict[str, Any] = Field(default_factory=dict)
    applicability_limitations: str = DEFAULT_APPLICABILITY

    @field_validator("jurisdiction")
    @classmethod
    def _normalize_jurisdiction(cls, value: str) -> str:
        code = (value or "").strip().upper()
        if not code:
            raise ValueError("jurisdiction is required")
        return code

    @model_validator(mode="after")
    def _check_verification_pairing(self) -> EvidenceItem:
        if self.source_type == EvidenceSourceType.INTERNAL_RAG and (
            self.verification_state
            != EvidenceVerificationState.UNVERIFIED_RETRIEVAL
        ):
            raise ValueError(
                "internal_rag evidence must use verification_state=unverified_retrieval"
            )
        if self.source_type == EvidenceSourceType.EXTERNAL_API and (
            self.verification_state
            != EvidenceVerificationState.UNVERIFIED_EXTERNAL
        ):
            raise ValueError(
                "external_api evidence must use verification_state=unverified_external"
            )
        return self


def build_evidence_id(
    *,
    source_type: EvidenceSourceType | str,
    jurisdiction: str,
    source_url: str | None = None,
    document_id: int | str | None = None,
    document_version_id: int | str | None = None,
    chunk_id: int | str | None = None,
    provider: str | None = None,
    passage_text: str = "",
) -> str:
    """Deterministic evidence identifier for later citation validation."""
    parts = [
        str(source_type.value if isinstance(source_type, Enum) else source_type),
        (jurisdiction or "").strip().upper(),
        str(provider or ""),
        str(document_id or ""),
        str(document_version_id or ""),
        str(chunk_id or ""),
        (source_url or "").strip().lower(),
        # Short content fingerprint guards against identical keys with different text.
        hashlib.sha256((passage_text or "").encode("utf-8")).hexdigest()[:16],
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"ev_{digest[:24]}"


def evidence_from_internal_passage(
    *,
    jurisdiction: str,
    official_url: str,
    document_id: int,
    document_version_id: int,
    chunk_id: int,
    content_text: str,
    score: float | None = None,
    heading_path: str | None = None,
    source_id: int | None = None,
    applicability_warning: str | None = None,
) -> EvidenceItem:
    """Map an internal RAG passage into the shared evidence contract."""
    evidence_id = build_evidence_id(
        source_type=EvidenceSourceType.INTERNAL_RAG,
        jurisdiction=jurisdiction,
        source_url=official_url,
        document_id=document_id,
        document_version_id=document_version_id,
        chunk_id=chunk_id,
        passage_text=content_text,
    )
    return EvidenceItem(
        evidence_id=evidence_id,
        source_type=EvidenceSourceType.INTERNAL_RAG,
        jurisdiction=jurisdiction,
        source_url=official_url,
        document_id=document_id,
        document_version_id=document_version_id,
        chunk_id=chunk_id,
        heading_path=heading_path,
        passage_text=content_text,
        retrieval_score=score,
        verification_state=EvidenceVerificationState.UNVERIFIED_RETRIEVAL,
        provenance={
            "origin": "internal_rag",
            "source_id": source_id,
        },
        applicability_limitations=applicability_warning or DEFAULT_APPLICABILITY,
    )


def evidence_from_external_item(
    *,
    jurisdiction: str | None,
    official_url: str,
    provider: str,
    passage_text: str,
    document_id: str | None = None,
    title: str | None = None,
    source_type_label: str | None = None,
) -> EvidenceItem:
    """Map an external API result into the shared evidence contract."""
    jur = (jurisdiction or "").strip().upper()
    if not jur:
        raise ValueError("external evidence requires a jurisdiction code")
    evidence_id = build_evidence_id(
        source_type=EvidenceSourceType.EXTERNAL_API,
        jurisdiction=jur,
        source_url=official_url,
        document_id=document_id,
        provider=provider,
        passage_text=passage_text,
    )
    return EvidenceItem(
        evidence_id=evidence_id,
        source_type=EvidenceSourceType.EXTERNAL_API,
        jurisdiction=jur,
        source_url=official_url,
        document_id=document_id,
        provider=provider,
        passage_text=passage_text,
        retrieval_score=None,
        verification_state=EvidenceVerificationState.UNVERIFIED_EXTERNAL,
        provenance={
            "origin": "external_api",
            "provider": provider,
            "title": title,
            "external_source_type": source_type_label,
        },
    )


RetrievalModeOption = Literal["semantic", "keyword", "hybrid"]
