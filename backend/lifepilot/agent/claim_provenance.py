"""Claim-level evidence provenance contracts (Phase 10C.4).

Builds additive, authentic evidence references for retained claims.
Never invents excerpts, document IDs, or human-review approvals.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field

from lifepilot.agent.evidence import EvidenceItem, EvidenceVerificationState
from lifepilot.agent.qualifiers import (
    extract_qualifier_profile,
    supporting_evidence_spans,
)

# Bound excerpt size in API / verification payloads.
_MAX_EXCERPT_CHARS = 480
_MAX_LIMITATIONS = 8


class EvidenceRefRole(str, Enum):
    CITED = "cited"
    CORROBORATING = "corroborating"


class ClaimEvidenceRef(BaseModel):
    """Structured reference from a claim to one supporting evidence item.

    Distinguishes machine-verified matching from human legal approval:
    ``verification_state`` / ``human_review_state`` report source review status;
    they do not certify that the claim itself is human-approved.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    role: EvidenceRefRole = EvidenceRefRole.CITED
    source_type: str | None = None
    source_url: str | None = None
    canonical_url: str | None = None
    source_name: str | None = None
    issuing_authority: str | None = None
    source_authority_category: str | None = None
    jurisdiction: str | None = None
    pathway_ids: list[str] = Field(default_factory=list)
    document_id: int | str | None = None
    document_version_id: int | str | None = None
    document_version_number: int | None = None
    chunk_id: int | str | None = None
    chunk_index: int | None = None
    heading_path: str | None = None
    excerpt: str | None = None
    excerpt_authenticated: bool = False
    excerpt_truncated: bool = False
    char_start: int | None = None
    char_end: int | None = None
    content_sha256: str | None = None
    source_published_at: str | None = None
    source_updated_at: str | None = None
    retrieved_at: str | None = None
    last_checked_at: str | None = None
    legal_status: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    cohort_key: str | None = None
    is_transitional: bool | None = None
    verification_state: str | None = None
    human_review_state: str | None = None
    is_truncated: bool = False
    truncation_reason: str | None = None
    provenance_limitations: list[str] = Field(default_factory=list)
    mechanical_match_only: bool = True


def _canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parts = urlparse(url)
        path = parts.path.rstrip("/") or ""
        return urlunparse(
            (parts.scheme.lower(), parts.netloc.lower(), path, "", "", "")
        )
    except Exception:  # noqa: BLE001
        return (url or "").strip() or None


def _human_review_state(item: EvidenceItem) -> str:
    state = item.verification_state
    if state == EvidenceVerificationState.HUMAN_VERIFIED_RULE:
        return "human_verified_source"
    if state == EvidenceVerificationState.UNVERIFIED_RETRIEVAL:
        return "unreviewed_retrieval"
    if state == EvidenceVerificationState.UNVERIFIED_EXTERNAL:
        return "unreviewed_external"
    return "unknown"


def _pathway_ids_for_item(item: EvidenceItem) -> list[str]:
    from lifepilot.agent.pathway_applicability import evidence_pathway_signals

    ids = list(evidence_pathway_signals(item))
    prov = item.provenance or {}
    raw = prov.get("pathway_id")
    if raw and str(raw) not in ids:
        ids.append(str(raw))
    return ids


def _temporal_fields(item: EvidenceItem) -> dict[str, Any]:
    tprov = item.temporal_provenance or {}
    return {
        "effective_from": tprov.get("effective_from") or item.explicit_effective_at,
        "effective_to": tprov.get("effective_to"),
        "cohort_key": tprov.get("cohort_key"),
        "is_transitional": (
            bool(tprov.get("is_transitional"))
            if "is_transitional" in tprov
            else None
        ),
    }


def derive_supporting_excerpt(
    claim: str,
    item: EvidenceItem,
    *,
    max_chars: int = _MAX_EXCERPT_CHARS,
) -> tuple[str | None, bool, bool, list[str]]:
    """Return (excerpt, authenticated, excerpt_truncated, limitations).

    Excerpt is always a real substring of ``item.passage_text`` when available.
    Never invents text. Bounded for response size.
    """
    limitations: list[str] = []
    passage = item.passage_text or ""
    if not passage.strip():
        return None, False, False, ["evidence_passage_empty"]

    spans = supporting_evidence_spans(claim, passage)
    candidate = " ".join(spans).strip() if spans else passage.strip()
    if not candidate:
        candidate = passage.strip()

    # Prefer the first supporting sentence when the join is huge.
    if len(candidate) > max_chars and spans:
        candidate = spans[0].strip()

    excerpt_truncated = False
    if len(candidate) > max_chars:
        # Truncate on whitespace when possible; mark as excerpt-bounded.
        cut = candidate[: max_chars + 1]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        else:
            cut = cut[:max_chars]
        candidate = cut.rstrip() + " …[excerpt_bounded]"
        excerpt_truncated = True
        limitations.append("excerpt_bounded_for_response_size")

    authenticated = candidate.replace(" …[excerpt_bounded]", "") in passage or (
        candidate in passage
    )
    # After bounding marker, authenticate the unmarked prefix.
    if not authenticated and candidate.endswith(" …[excerpt_bounded]"):
        prefix = candidate[: -len(" …[excerpt_bounded]")]
        authenticated = prefix in passage

    if not authenticated:
        # Fall back to a bounded prefix of the authentic passage.
        raw = passage.strip()
        if len(raw) > max_chars:
            cut = raw[: max_chars + 1]
            if " " in cut:
                cut = cut.rsplit(" ", 1)[0]
            else:
                cut = cut[:max_chars]
            candidate = cut.rstrip() + " …[excerpt_bounded]"
            excerpt_truncated = True
            limitations.append("excerpt_bounded_for_response_size")
        else:
            candidate = raw
        authenticated = True

    if getattr(item, "is_truncated", False) or "…[truncated]" in passage:
        limitations.append("source_passage_truncated_conditions_may_be_omitted")
        excerpt_truncated = True

    return candidate, authenticated, excerpt_truncated, limitations


def build_claim_evidence_ref(
    claim: str,
    item: EvidenceItem,
    *,
    role: EvidenceRefRole = EvidenceRefRole.CITED,
    include_excerpt: bool = True,
) -> ClaimEvidenceRef:
    """Build one claim↔evidence reference from a real EvidenceItem."""
    limitations: list[str] = []
    temporal = _temporal_fields(item)

    excerpt: str | None = None
    authenticated = False
    excerpt_truncated = False
    if include_excerpt:
        excerpt, authenticated, excerpt_truncated, ex_lim = derive_supporting_excerpt(
            claim, item
        )
        limitations.extend(ex_lim)
    else:
        if getattr(item, "is_truncated", False) or "…[truncated]" in (item.passage_text or ""):
            limitations.append("source_passage_truncated_conditions_may_be_omitted")
            excerpt_truncated = True

    if item.document_id is None and item.source_type.value == "external_api":
        limitations.append("external_evidence_document_id_unavailable")
    if item.chunk_id is None and item.source_type.value == "external_api":
        limitations.append("external_evidence_chunk_id_unavailable")
    if item.verification_state != EvidenceVerificationState.HUMAN_VERIFIED_RULE:
        limitations.append("source_not_human_verified_for_claim")
    else:
        limitations.append(
            "human_verified_source_does_not_imply_claim_level_legal_approval"
        )

    profile = extract_qualifier_profile(item.passage_text or "")
    if profile.is_conditional or profile.cohorts:
        limitations.append("evidence_contains_conditional_or_cohort_scope")
    if profile.unrecognized_condition_markers:
        limitations.append("evidence_contains_unrecognized_condition_markers")

    # Dedupe limitations, preserve order.
    seen: set[str] = set()
    uniq_lim: list[str] = []
    for lim in limitations:
        if lim not in seen:
            seen.add(lim)
            uniq_lim.append(lim)

    return ClaimEvidenceRef(
        evidence_id=item.evidence_id,
        role=role,
        source_type=item.source_type.value if item.source_type else None,
        source_url=item.source_url,
        canonical_url=_canonical_url(item.source_url),
        source_name=item.source_name,
        issuing_authority=item.issuing_authority or item.authority_name,
        source_authority_category=getattr(
            item, "source_authority_category", None
        ),
        jurisdiction=item.jurisdiction,
        pathway_ids=_pathway_ids_for_item(item),
        document_id=item.document_id,
        document_version_id=item.document_version_id,
        document_version_number=item.document_version_number,
        chunk_id=item.chunk_id,
        chunk_index=item.chunk_index,
        heading_path=item.heading_path,
        excerpt=excerpt,
        excerpt_authenticated=authenticated if include_excerpt else False,
        excerpt_truncated=excerpt_truncated,
        char_start=item.char_start,
        char_end=item.char_end,
        content_sha256=item.content_sha256,
        source_published_at=item.source_published_at,
        source_updated_at=item.source_updated_at,
        retrieved_at=item.retrieved_at,
        last_checked_at=item.last_checked_at,
        legal_status=item.legal_status,
        effective_from=temporal.get("effective_from"),
        effective_to=temporal.get("effective_to"),
        cohort_key=(
            str(temporal["cohort_key"])
            if temporal.get("cohort_key") is not None
            else None
        ),
        is_transitional=temporal.get("is_transitional"),
        verification_state=(
            item.verification_state.value if item.verification_state else None
        ),
        human_review_state=_human_review_state(item),
        is_truncated=bool(getattr(item, "is_truncated", False)),
        truncation_reason=item.truncation_reason,
        provenance_limitations=uniq_lim[:_MAX_LIMITATIONS],
        mechanical_match_only=(
            item.verification_state != EvidenceVerificationState.HUMAN_VERIFIED_RULE
        ),
    )


def build_claim_evidence_refs(
    claim: str,
    cited: Sequence[EvidenceItem],
    *,
    corroborating: EvidenceItem | None = None,
    include_excerpt: bool = True,
) -> list[ClaimEvidenceRef]:
    refs = [
        build_claim_evidence_ref(
            claim, item, role=EvidenceRefRole.CITED, include_excerpt=include_excerpt
        )
        for item in cited
    ]
    if corroborating is not None:
        refs.append(
            build_claim_evidence_ref(
                claim,
                corroborating,
                role=EvidenceRefRole.CORROBORATING,
                include_excerpt=include_excerpt,
            )
        )
    return refs


def project_evidence_item_for_api(
    item: EvidenceItem,
    *,
    detailed: bool,
) -> dict[str, Any]:
    """Compact vs detailed HTTP evidence projection (no secrets / stack traces)."""
    base: dict[str, Any] = {
        "evidence_id": item.evidence_id,
        "source_type": item.source_type.value,
        "verification_state": item.verification_state.value,
        "source_url": item.source_url,
        "jurisdiction": item.jurisdiction,
        "source_authority_category": getattr(
            item, "source_authority_category", "unknown"
        ),
        "issuing_authority": item.issuing_authority or item.authority_name,
        "legal_status": item.legal_status,
        "is_truncated": bool(getattr(item, "is_truncated", False)),
        "human_review_state": _human_review_state(item),
    }
    if not detailed:
        return base

    temporal = _temporal_fields(item)
    detailed_fields: dict[str, Any] = {
        "source_name": item.source_name,
        "title": item.title,
        "heading_path": item.heading_path,
        "document_id": item.document_id,
        "document_version_id": item.document_version_id,
        "document_version_number": item.document_version_number,
        "chunk_id": item.chunk_id,
        "chunk_index": item.chunk_index,
        "char_start": item.char_start,
        "char_end": item.char_end,
        "content_sha256": item.content_sha256,
        "source_published_at": item.source_published_at,
        "source_updated_at": item.source_updated_at,
        "retrieved_at": item.retrieved_at,
        "last_checked_at": item.last_checked_at,
        "truncation_reason": item.truncation_reason,
        "pathway_ids": _pathway_ids_for_item(item),
        "effective_from": temporal.get("effective_from"),
        "effective_to": temporal.get("effective_to"),
        "cohort_key": temporal.get("cohort_key"),
        "is_transitional": temporal.get("is_transitional"),
        "passage_text": _bounded_passage(item.passage_text or ""),
    }
    base.update(detailed_fields)
    return base


def _bounded_passage(text: str, max_chars: int = 1200) -> str:
    blob = (text or "").strip()
    if len(blob) <= max_chars:
        return blob
    cut = blob[: max_chars + 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    else:
        cut = cut[:max_chars]
    return cut.rstrip() + " …[truncated]"


def project_claim_verification_for_api(
    outcomes: Iterable[Any],
    *,
    include_excerpts: bool,
    retained_only: bool = True,
) -> list[dict[str, Any]]:
    """Project claim outcomes for HTTP. Rejected claims omitted by default."""
    out: list[dict[str, Any]] = []
    for oc in outcomes or []:
        retained = bool(getattr(oc, "retained", False))
        if retained_only and not retained:
            continue
        dump = oc.model_dump(mode="json") if hasattr(oc, "model_dump") else dict(oc)
        # Strip suppressed-claim leakage: only retained (or explicitly requested).
        refs = dump.get("evidence_refs") or []
        if not include_excerpts:
            cleaned_refs = []
            for ref in refs:
                r = dict(ref)
                r.pop("excerpt", None)
                r["excerpt"] = None
                r["excerpt_authenticated"] = False
                cleaned_refs.append(r)
            dump["evidence_refs"] = cleaned_refs
        # Distinguish mechanical support from human legal approval (Phase 10D.4).
        refs_for_label = dump.get("evidence_refs") or []
        legal_authority = "mechanically_supported_or_unverified"
        for ref in refs_for_label:
            if isinstance(ref, dict) and ref.get("verification_state") == (
                EvidenceVerificationState.HUMAN_VERIFIED_RULE.value
            ):
                legal_authority = "human_verified_rule"
                break
            if isinstance(ref, dict) and ref.get("human_review_state") == (
                "human_verified_source"
            ):
                legal_authority = "human_verified_rule"
                break

        # Never expose internal stack / cost internals beyond report metrics.
        out.append(
            {
                "claim": dump.get("claim"),
                "evidence_ids": dump.get("evidence_ids") or [],
                "status": dump.get("status"),
                "retained": retained,
                "reasons": dump.get("reasons") or [],
                "original_uncertainty": dump.get("original_uncertainty"),
                "revised_uncertainty": dump.get("revised_uncertainty"),
                "independent_corroboration": dump.get("independent_corroboration"),
                "corroborating_evidence_id": dump.get("corroborating_evidence_id"),
                "provenance_incomplete": dump.get("provenance_incomplete", False),
                "evidence_refs": dump.get("evidence_refs") or [],
                "legal_authority_label": legal_authority,
            }
        )
    return out


_DOC_ID_RE = re.compile(r"^\d+$")


def same_underlying_document(a: EvidenceItem, b: EvidenceItem) -> bool:
    """True when two items are the same official document / mirrored copy."""
    if a.evidence_id == b.evidence_id:
        return True
    if (
        a.document_version_id is not None
        and b.document_version_id is not None
        and str(a.document_version_id) == str(b.document_version_id)
    ):
        return True
    if (
        a.document_id is not None
        and b.document_id is not None
        and str(a.document_id) == str(b.document_id)
    ):
        return True
    if (
        a.content_sha256
        and b.content_sha256
        and a.content_sha256 == b.content_sha256
    ):
        return True
    ca = _canonical_url(a.source_url)
    cb = _canonical_url(b.source_url)
    if ca and cb and ca == cb:
        return True
    return False
