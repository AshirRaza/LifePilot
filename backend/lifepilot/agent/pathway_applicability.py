"""Deterministic pathway applicability checks for grounded claims.

Prevents numerical requirements from being treated as applicable to a pathway
merely because they appear in a retrieved document about a different pathway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

from lifepilot.agent.evidence import EvidenceItem

# Markers are lowercase phrases / URL fragments associated with inventory pathway IDs.
# Prefer extending this map from inventory aliases rather than inventing new pathway IDs.
# URL path fragments are authoritative when present — a GEP page that mentions the
# Critical Skills Occupations List must not be treated as Critical Skills evidence.
_PATHWAY_URL_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "ie_critical_skills_employment_permit": ("/critical-skills", "critical-skills-employment"),
    "ie_general_employment_permit": ("/general-employment", "general-employment-permit"),
    "gb_skilled_worker": ("/skilled-worker-visa",),
    "gb_student": ("/student-visa",),
}

_PATHWAY_MARKERS: dict[str, tuple[str, ...]] = {
    "ie_critical_skills_employment_permit": (
        "critical skills employment permit",
        "critical skills occupations list",
        "critical skills",
        "/critical-skills",
        "csep",
    ),
    "ie_general_employment_permit": (
        "general employment permit",
        "/general-employment",
        "gep",
    ),
    "gb_skilled_worker": (
        "skilled worker visa",
        "skilled worker",
        "/skilled-worker-visa",
    ),
    "gb_student": (
        "student visa",
        "/student-visa",
    ),
    "nl_highly_skilled_migrant": (
        "highly skilled migrant",
        "kennismigrant",
    ),
    "sg_employment_pass": (
        "employment pass",
        "/employment-pass",
    ),
    "fr_passeport_talent": (
        "passeport talent",
        "carte talent",
        "talent card",
    ),
}

_COMPARISON_RE = re.compile(
    r"(?i)\b("
    r"compared with|compared to|unlike|whereas|by contrast|distinct from|"
    r"separate from|different from|as opposed to|rather than|"
    r"in contrast to|as distinct from"
    r")\b"
)
_NUMERIC_RE = re.compile(
    r"(?:£|\$|€)\s?\d[\d,]*(?:\.\d+)?|"
    r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b"
)


class PathwayApplicability(str, Enum):
    DIRECT = "direct"
    EXPLICIT_COMPARISON = "explicit_comparison"
    RELATED_CONTEXT = "related_context"
    CROSS_PATHWAY = "cross_pathway"
    INSUFFICIENT = "insufficient"
    NOT_APPLICABLE = "not_applicable"  # no pathway hint on request


@dataclass(frozen=True)
class PathwayApplicabilityResult:
    classification: PathwayApplicability
    reason: str
    requested_pathway_id: str | None
    pathways_in_claim: tuple[str, ...]
    pathways_in_evidence: tuple[str, ...]


@lru_cache(maxsize=1)
def pathway_marker_map() -> dict[str, tuple[str, ...]]:
    return dict(_PATHWAY_MARKERS)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def has_explicit_comparison_language(text: str) -> bool:
    """True when the claim uses explicit cross-pathway comparison wording."""
    return bool(_COMPARISON_RE.search(text or ""))


def detect_pathway_ids(text: str) -> list[str]:
    """Return pathway IDs whose markers appear in text (longest-marker preference)."""
    blob = _normalize(text)
    if not blob:
        return []
    found: list[str] = []
    for pathway_id, markers in pathway_marker_map().items():
        ordered = sorted(markers, key=len, reverse=True)
        if any(m in blob for m in ordered):
            found.append(pathway_id)
    return found


def pathway_ids_from_url(url: str | None) -> list[str]:
    blob = (url or "").lower()
    if not blob:
        return []
    hits: list[str] = []
    for pathway_id, frags in _PATHWAY_URL_FRAGMENTS.items():
        if any(f in blob for f in frags):
            hits.append(pathway_id)
    return hits


def evidence_pathway_signals(item: EvidenceItem) -> list[str]:
    """Pathway signals for an evidence item.

    When the official URL clearly identifies a pathway page, URL signals win so
    cross-mentions (e.g. GEP text referencing the Critical Skills list) cannot
    re-attribute that page's figures.
    """
    url_hits = pathway_ids_from_url(item.source_url)
    if url_hits:
        return url_hits
    parts = [
        item.passage_text or "",
        item.heading_path or "",
        item.source_url or "",
        str((item.provenance or {}).get("title") or ""),
    ]
    return detect_pathway_ids(" ".join(parts))


def normalize_numeric_literal(value: str) -> str:
    """Strip spaces/commas so £38,700 and £38700 compare equal."""
    return re.sub(r"[\s,]", "", value or "")


def numeric_literals_in_text(text: str) -> list[str]:
    return _NUMERIC_RE.findall(text or "")


def passage_contains_literal(passage: str, literal: str) -> bool:
    compact_passage = normalize_numeric_literal(passage or "")
    compact_lit = normalize_numeric_literal(literal)
    if compact_lit and compact_lit in compact_passage:
        return True
    digits = re.sub(r"[^\d.]", "", literal or "")
    return bool(digits and digits in compact_passage)


def number_supported_by_requested_pathway_passage(
    *,
    claim: str,
    cited: list[EvidenceItem],
    pathway_id: str,
) -> bool:
    """True when each claim numeric appears in a passage that marks the requested pathway.

    Prevents citing a Critical Skills page *and* a GEP salary passage, then treating
    the GEP figure as a Critical Skills requirement (F07 / X05 class failures).
    """
    literals = numeric_literals_in_text(claim)
    if not literals:
        return True
    requested = (pathway_id or "").strip()
    if not requested:
        return True
    for lit in literals:
        passages_with_lit = [
            item for item in cited if passage_contains_literal(item.passage_text or "", lit)
        ]
        if not passages_with_lit:
            # Literal absent from citations — handled by numeric grounding elsewhere.
            continue
        if not any(requested in evidence_pathway_signals(item) for item in passages_with_lit):
            return False
    return True


def classify_pathway_applicability(
    *,
    claim: str,
    cited: list[EvidenceItem],
    pathway_id: str | None,
) -> PathwayApplicabilityResult:
    """Classify whether cited evidence can support a pathway-scoped claim."""
    requested = (pathway_id or "").strip() or None
    if not requested:
        return PathwayApplicabilityResult(
            classification=PathwayApplicability.NOT_APPLICABLE,
            reason="no_pathway_hint",
            requested_pathway_id=None,
            pathways_in_claim=tuple(),
            pathways_in_evidence=tuple(),
        )

    claim_pathways = tuple(detect_pathway_ids(claim))
    evidence_pathways: list[str] = []
    for item in cited:
        for pid in evidence_pathway_signals(item):
            if pid not in evidence_pathways:
                evidence_pathways.append(pid)
    evidence_tuple = tuple(evidence_pathways)

    has_number = bool(_NUMERIC_RE.search(claim or ""))
    # Only explicit comparison language counts — merely naming two pathways must
    # not bypass numeric locality (F07/X05 class failures).
    comparison = has_explicit_comparison_language(claim)

    # Explicit comparison involving the requested pathway.
    if comparison and (
        requested in claim_pathways
        or requested in evidence_tuple
        or any(p != requested for p in claim_pathways)
    ):
        # Still require that cited evidence mentions the foreign pathway being compared
        # when the claim names that pathway.
        foreign = [p for p in claim_pathways if p != requested]
        if foreign and not any(p in evidence_tuple for p in foreign):
            return PathwayApplicabilityResult(
                classification=PathwayApplicability.INSUFFICIENT,
                reason="comparison_lacks_cited_foreign_pathway_evidence",
                requested_pathway_id=requested,
                pathways_in_claim=claim_pathways,
                pathways_in_evidence=evidence_tuple,
            )
        return PathwayApplicabilityResult(
            classification=PathwayApplicability.EXPLICIT_COMPARISON,
            reason="explicit_cross_pathway_comparison",
            requested_pathway_id=requested,
            pathways_in_claim=claim_pathways,
            pathways_in_evidence=evidence_tuple,
        )

    # Claim clearly about another pathway only → related context (not a drop).
    foreign_only_claim = (
        has_number
        and bool(claim_pathways)
        and requested not in claim_pathways
        and all(p != requested for p in claim_pathways)
    )
    if foreign_only_claim:
        return PathwayApplicabilityResult(
            classification=PathwayApplicability.RELATED_CONTEXT,
            reason="claim_about_other_pathway_only",
            requested_pathway_id=requested,
            pathways_in_claim=claim_pathways,
            pathways_in_evidence=evidence_tuple,
        )

    # Numeric locality for requested-pathway (or unscoped) claims: the figure must
    # appear in a passage that also marks the requested pathway.
    if has_number and not number_supported_by_requested_pathway_passage(
        claim=claim,
        cited=cited,
        pathway_id=requested,
    ):
        return PathwayApplicabilityResult(
            classification=PathwayApplicability.CROSS_PATHWAY,
            reason="numeric_literal_not_in_requested_pathway_passage",
            requested_pathway_id=requested,
            pathways_in_claim=claim_pathways,
            pathways_in_evidence=evidence_tuple,
        )

    # Direct support: evidence mentions requested pathway.
    if requested in evidence_tuple:
        return PathwayApplicabilityResult(
            classification=PathwayApplicability.DIRECT,
            reason="evidence_mentions_requested_pathway",
            requested_pathway_id=requested,
            pathways_in_claim=claim_pathways,
            pathways_in_evidence=evidence_tuple,
        )

    # Numeric claim with only other-pathway evidence → cross-pathway attribution risk.
    if has_number and evidence_tuple and requested not in evidence_tuple:
        # Claim that clearly names only the other pathway as owner of the number
        # can be kept as related context (not as a requested-pathway rule) when
        # the claim itself attributes correctly — callers may keep RELATED_CONTEXT.
        if claim_pathways and requested not in claim_pathways:
            return PathwayApplicabilityResult(
                classification=PathwayApplicability.RELATED_CONTEXT,
                reason="other_pathway_rule_with_matching_claim_attribution",
                requested_pathway_id=requested,
                pathways_in_claim=claim_pathways,
                pathways_in_evidence=evidence_tuple,
            )
        return PathwayApplicabilityResult(
            classification=PathwayApplicability.CROSS_PATHWAY,
            reason="numeric_claim_lacks_requested_pathway_evidence",
            requested_pathway_id=requested,
            pathways_in_claim=claim_pathways,
            pathways_in_evidence=evidence_tuple,
        )

    if not evidence_tuple:
        return PathwayApplicabilityResult(
            classification=PathwayApplicability.INSUFFICIENT,
            reason="no_pathway_markers_in_evidence",
            requested_pathway_id=requested,
            pathways_in_claim=claim_pathways,
            pathways_in_evidence=evidence_tuple,
        )

    return PathwayApplicabilityResult(
        classification=PathwayApplicability.RELATED_CONTEXT,
        reason="non_numeric_or_ambiguous_pathway_context",
        requested_pathway_id=requested,
        pathways_in_claim=claim_pathways,
        pathways_in_evidence=evidence_tuple,
    )


def should_drop_for_pathway(
    result: PathwayApplicabilityResult,
    *,
    claim: str,
) -> bool:
    """Drop only risky numeric cross-pathway attributions when a pathway is requested."""
    if result.requested_pathway_id is None:
        return False
    if result.classification == PathwayApplicability.CROSS_PATHWAY:
        return True
    # Ambiguous numeric applicability with no pathway markers in evidence.
    if (
        result.classification == PathwayApplicability.INSUFFICIENT
        and bool(_NUMERIC_RE.search(claim or ""))
        and result.reason == "no_pathway_markers_in_evidence"
    ):
        return True
    return False
