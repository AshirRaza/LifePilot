"""Evidence and citation contracts shared by internal RAG and external APIs."""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lifepilot.agent.provenance import (
    SourceAuthorityCategory,
    classify_source_authority,
    locale_hint_from_language,
    normalize_language_tag,
)


class EvidenceSourceType(str, Enum):
    INTERNAL_RAG = "internal_rag"
    EXTERNAL_API = "external_api"
    CURATED_RULE = "curated_rule"


class EvidenceVerificationState(str, Enum):
    """Distinct verification labels — never conflate retrieval with COMPLETE."""

    UNVERIFIED_RETRIEVAL = "unverified_retrieval"
    UNVERIFIED_EXTERNAL = "unverified_external"
    HUMAN_VERIFIED_RULE = "human_verified_rule"


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
    source_id: int | str | None = None
    authority_id: int | str | None = None
    authority_name: str | None = None
    source_name: str | None = None
    fetched_via_source_id: int | str | None = None
    document_id: int | str | None = None
    document_version_id: int | str | None = None
    document_version_number: int | None = None
    chunk_id: int | str | None = None
    chunk_index: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    content_sha256: str | None = None
    provider: str | None = None
    heading_path: str | None = None
    title: str | None = None
    passage_text: str = Field(..., min_length=1)
    retrieval_score: float | None = None
    verification_state: EvidenceVerificationState
    language: str | None = None
    locale_hint: str | None = None
    source_authority_category: str = SourceAuthorityCategory.UNKNOWN.value
    issuing_authority: str | None = None
    is_truncated: bool = False
    truncation_reason: str | None = None
    retrieved_at: str | None = None
    last_checked_at: str | None = None
    http_last_modified: str | None = None
    source_published_at: str | None = None
    source_updated_at: str | None = None
    explicit_effective_at: str | None = None
    legal_status: str | None = None
    temporal_provenance: dict[str, Any] = Field(default_factory=dict)
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
        if self.source_type == EvidenceSourceType.CURATED_RULE and (
            self.verification_state
            != EvidenceVerificationState.HUMAN_VERIFIED_RULE
        ):
            raise ValueError(
                "curated_rule evidence must use verification_state=human_verified_rule"
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
    source_name: str | None = None,
    authority_id: int | None = None,
    authority_name: str | None = None,
    fetched_via_source_id: int | None = None,
    document_version_number: int | None = None,
    chunk_index: int | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
    content_sha256: str | None = None,
    version_content_sha256: str | None = None,
    language: str | None = None,
    applicability_warning: str | None = None,
    title: str | None = None,
    retrieved_at: str | None = None,
    last_checked_at: str | None = None,
    http_last_modified: str | None = None,
    source_published_at: str | None = None,
    source_updated_at: str | None = None,
    legal_status: str | None = None,
    is_current_version: bool = True,
    is_truncated: bool = False,
    truncation_reason: str | None = None,
    temporal_provenance: dict[str, Any] | None = None,
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
    norm_lang = normalize_language_tag(language)
    loc_hint = locale_hint_from_language(norm_lang)
    auth_cat, issuing = classify_source_authority(
        source_type=EvidenceSourceType.INTERNAL_RAG.value,
        source_url=official_url,
        legal_status=legal_status,
        authority_name=authority_name,
        source_name=source_name,
        authority_id=authority_id,
    )
    tprov = dict(temporal_provenance or {})
    tprov.setdefault("origin", "internal_rag")
    tprov.setdefault("effective_at_known", False)
    tprov["is_current_version"] = bool(is_current_version)
    trunc_flag = bool(is_truncated or "…[truncated]" in (content_text or ""))
    return EvidenceItem(
        evidence_id=evidence_id,
        source_type=EvidenceSourceType.INTERNAL_RAG,
        jurisdiction=jurisdiction,
        source_url=official_url,
        source_id=source_id,
        authority_id=authority_id,
        authority_name=authority_name,
        source_name=source_name,
        fetched_via_source_id=fetched_via_source_id,
        document_id=document_id,
        document_version_id=document_version_id,
        document_version_number=document_version_number,
        chunk_id=chunk_id,
        chunk_index=chunk_index,
        char_start=char_start,
        char_end=char_end,
        content_sha256=content_sha256 or version_content_sha256,
        heading_path=heading_path,
        title=title,
        passage_text=content_text,
        retrieval_score=score,
        verification_state=EvidenceVerificationState.UNVERIFIED_RETRIEVAL,
        language=norm_lang,
        locale_hint=loc_hint,
        source_authority_category=auth_cat.value,
        issuing_authority=issuing,
        is_truncated=trunc_flag,
        truncation_reason=truncation_reason or ("excerpt_truncated" if trunc_flag else None),
        retrieved_at=retrieved_at,
        last_checked_at=last_checked_at,
        http_last_modified=http_last_modified,
        source_published_at=source_published_at,
        source_updated_at=source_updated_at,
        explicit_effective_at=None,
        legal_status=legal_status or "unknown",
        temporal_provenance=tprov,
        provenance={
            "origin": "internal_rag",
            "source_id": source_id,
            "source_name": source_name,
            "authority_id": authority_id,
            "authority_name": authority_name,
            "fetched_via_source_id": fetched_via_source_id,
            "document_version_number": document_version_number,
            "chunk_index": chunk_index,
            "char_start": char_start,
            "char_end": char_end,
            "chunk_content_sha256": content_sha256,
            "version_content_sha256": version_content_sha256,
            "language": norm_lang,
            "locale_hint": loc_hint,
            "source_authority_category": auth_cat.value,
            "issuing_authority": issuing,
            "title": title,
            "is_current_version": bool(is_current_version),
            "is_truncated": trunc_flag,
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
    canonical_url: str | None = None,
    title: str | None = None,
    source_type_label: str | None = None,
    published_at: str | None = None,
    updated_at: str | None = None,
    effective_at: str | None = None,
    retrieved_at: str | None = None,
    http_last_modified: str | None = None,
    legal_status: str | None = None,
    language: str | None = None,
    metadata: dict[str, Any] | None = None,
    temporal_provenance: dict[str, Any] | None = None,
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
    meta = dict(metadata or {})
    norm_lang = normalize_language_tag(language)
    loc_hint = locale_hint_from_language(norm_lang)
    auth_cat, issuing = classify_source_authority(
        source_type=EvidenceSourceType.EXTERNAL_API.value,
        source_url=official_url or canonical_url,
        provider=provider,
        external_source_type=source_type_label,
        legal_status=legal_status,
        metadata=meta,
    )
    trunc_flag = bool(
        meta.get("has_truncated_passages")
        or "…[truncated]" in (passage_text or "")
    )
    tprov = dict(temporal_provenance or {})
    tprov.setdefault("origin", "external_api")
    tprov.setdefault("provider", provider)
    tprov["effective_at_is_connector_assertion"] = bool(effective_at)
    tprov["legal_status_is_connector_assertion"] = bool(legal_status)
    return EvidenceItem(
        evidence_id=evidence_id,
        source_type=EvidenceSourceType.EXTERNAL_API,
        jurisdiction=jur,
        source_url=official_url,
        document_id=document_id,
        provider=provider,
        title=title,
        passage_text=passage_text,
        retrieval_score=None,
        verification_state=EvidenceVerificationState.UNVERIFIED_EXTERNAL,
        language=norm_lang,
        locale_hint=loc_hint,
        source_authority_category=auth_cat.value,
        issuing_authority=issuing,
        is_truncated=trunc_flag,
        truncation_reason=(
            "external_passage_truncated" if trunc_flag else None
        ),
        retrieved_at=retrieved_at,
        http_last_modified=http_last_modified,
        source_published_at=published_at,
        source_updated_at=updated_at,
        explicit_effective_at=effective_at,
        legal_status=legal_status or "unknown",
        temporal_provenance=tprov,
        provenance={
            "origin": "external_api",
            "provider": provider,
            "title": title,
            "canonical_url": canonical_url or official_url,
            "external_source_type": source_type_label,
            "language": norm_lang,
            "locale_hint": loc_hint,
            "source_authority_category": auth_cat.value,
            "issuing_authority": issuing,
            "eu_level_source": bool(
                jur == "EU"
                or meta.get("eu_level_source")
                or provider in {"eurlex_cellar", "eu_opendata", "eu_immigration_portal"}
            ),
            "is_truncated": trunc_flag,
            "is_consolidated": meta.get("is_consolidated"),
            "celex": meta.get("celex"),
            "document_type": meta.get("type") or meta.get("document_type"),
        },
    )


def evidence_from_legal_rule(rule: Any, session: Any | None = None) -> EvidenceItem:
    """Map a human-verified LegalRule row into the shared evidence contract.

    When ``session`` is provided, Phase 10D.4 approval-invariant validation is
    required — standalone DB flags cannot upgrade unapproved rules.
    """
    if (
        getattr(rule, "review_status", None) != "approved"
        or getattr(rule, "verification_status", None) != "human_verified"
    ):
        raise ValueError("Only approved and human_verified LegalRule rows may become curated_rule evidence")
    if session is not None:
        from lifepilot.governance.approval_invariant import (
            validate_legal_rule_human_authority,
        )

        check = validate_legal_rule_human_authority(session, rule)
        if not check.ok:
            raise ValueError(
                "LegalRule failed approval invariant: "
                + ",".join(check.reasons)
            )

    parts = [rule.rule_summary.strip()]
    if rule.numeric_value is not None:
        comp = rule.numeric_comparator or ""
        cur = f"{rule.currency_code} " if rule.currency_code else ""
        unit = f" ({rule.amount_unit})" if rule.amount_unit else ""
        band = (
            f" to {cur}{rule.numeric_max_value}"
            if rule.numeric_max_value is not None
            else ""
        )
        prefix = f"{comp} " if comp else ""
        parts.append(f"Requirement figure: {prefix}{cur}{rule.numeric_value}{band}{unit}.")

    from_b = "[" if rule.effective_from_inclusive else "("
    to_b = "]" if rule.effective_to_inclusive else ")"
    eff_from_s = rule.effective_from.isoformat() if rule.effective_from else "unknown"
    eff_to_s = rule.effective_to.isoformat() if rule.effective_to else "open-ended"
    parts.append(
        f"Effective period: {from_b}{eff_from_s}, {eff_to_s}{to_b} "
        f"(status={rule.effective_date_status})."
    )
    parts.append(
        f"Cohort: {rule.cohort_key} (category={rule.cohort_category}, "
        f"transitional={bool(rule.is_transitional)})."
    )
    if rule.conditions_text:
        parts.append(f"Conditions: {rule.conditions_text.strip()}")
    if rule.supporting_excerpt:
        parts.append(f"Official excerpt: {rule.supporting_excerpt.strip()}")
    passage_text = " ".join(p for p in parts if p)

    eff_window = (
        f"{from_b}{eff_from_s}, {eff_to_s}{to_b}"
        if rule.effective_from is not None
        else None
    )
    eid = build_evidence_id(
        source_type=EvidenceSourceType.CURATED_RULE,
        jurisdiction=rule.jurisdiction_code,
        source_url=rule.source_url,
        document_id=rule.document_id,
        document_version_id=rule.document_version_id,
        provider=f"{rule.rule_key}:r{rule.rule_revision}",
        passage_text=passage_text,
    )
    return EvidenceItem(
        evidence_id=eid,
        source_type=EvidenceSourceType.CURATED_RULE,
        jurisdiction=rule.jurisdiction_code,
        source_url=rule.source_url,
        source_id=getattr(rule, "source_id", None),
        document_id=rule.document_id,
        document_version_id=rule.document_version_id,
        chunk_id=None,
        provider="curated_legal_rule",
        heading_path=f"{rule.pathway_id or rule.jurisdiction_code} > {rule.requirement_category}",
        title=f"Curated Rule: {rule.rule_key} (rev {rule.rule_revision})",
        passage_text=passage_text,
        retrieval_score=1.0,
        verification_state=EvidenceVerificationState.HUMAN_VERIFIED_RULE,
        source_authority_category=SourceAuthorityCategory.OFFICIAL_BINDING_RULES.value,
        issuing_authority="Curated LegalRule",
        retrieved_at=rule.reviewed_at.isoformat() if rule.reviewed_at else None,
        explicit_effective_at=eff_window,
        legal_status=rule.effective_date_status,
        temporal_provenance={
            "origin": "curated_rule",
            "rule_id": rule.id,
            "rule_key": rule.rule_key,
            "rule_revision": rule.rule_revision,
            "effective_from": rule.effective_from.isoformat() if rule.effective_from else None,
            "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
            "effective_from_inclusive": bool(rule.effective_from_inclusive),
            "effective_to_inclusive": bool(rule.effective_to_inclusive),
            "effective_date_status": rule.effective_date_status,
            "cohort_key": rule.cohort_key,
            "cohort_category": rule.cohort_category,
            "is_transitional": bool(rule.is_transitional),
            "reviewed_by": rule.reviewed_by,
            "reviewed_at": rule.reviewed_at.isoformat() if rule.reviewed_at else None,
            "effective_at_known": True,
        },
        provenance={
            "origin": "curated_rule",
            "rule_key": rule.rule_key,
            "rule_revision": rule.rule_revision,
            "pathway_id": rule.pathway_id,
            "requirement_category": rule.requirement_category,
            "requirement_type": rule.requirement_type,
            "cohort_key": rule.cohort_key,
            "source_id": rule.source_id,
            "reviewed_by": rule.reviewed_by,
            "numeric_value": (
                str(rule.numeric_value) if rule.numeric_value is not None else None
            ),
            "numeric_max_value": (
                str(rule.numeric_max_value)
                if rule.numeric_max_value is not None
                else None
            ),
            "numeric_comparator": rule.numeric_comparator,
            "currency_code": rule.currency_code,
            "amount_unit": rule.amount_unit,
        },
        applicability_limitations=(
            "Human-verified curated legal rule scoped to its effective period and "
            "applicant cohort; verify personal circumstances before applying."
        ),
    )


def evidence_locale_hint(item: EvidenceItem) -> str | None:
    """Return reliable numeric locale_hint from explicit EvidenceItem language/locale."""
    if item.locale_hint:
        return item.locale_hint
    if item.language:
        return locale_hint_from_language(item.language)
    return None


RetrievalModeOption = Literal["semantic", "keyword", "hybrid"]
