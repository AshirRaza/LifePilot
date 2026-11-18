"""Deterministic pathway applicability checks for grounded claims.

Prevents numerical requirements from being treated as applicable to a pathway
merely because they appear in a retrieved document about a different pathway.

Consolidates URL tokens, hard exclusions, and pathway body markers from
``coverage.corpus_reliability.pathway_applicability`` and
``config/coverage/pathway_seeds.json`` without speculative guessing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

from lifepilot.agent.evidence import EvidenceItem, EvidenceSourceType
from lifepilot.agent.numeric import (
    diagnose_claim_numeric_support,
    extract_numeric_quantities,
    format_canonical_text_literals,
    parse_single_quantity,
    quantities_from_evidence_item,
    quantity_matches_evidence,
)
from lifepilot.coverage.corpus_reliability.pathway_applicability import (
    PATHWAY_BODY_MARKERS,
    PATHWAY_HARD_EXCLUSIONS,
    PATHWAY_URL_TOKENS,
    url_key,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PATHWAY_SEEDS_PATH = _REPO_ROOT / "config" / "coverage" / "pathway_seeds.json"

# Generic URL hubs or overly short tokens in seeds that must not become pathway signals alone.
_IGNORED_SEED_URL_TOKENS = frozenset(
    {
        "/work",
        "/study",
        "/live",
        "/familie/",
        "/arbeit/",
        "citizenship",
        "/working-in-the-united-states",
    }
)

# Short acronyms that require word-boundary matching in body text.
_BOUNDARY_ONLY_MARKERS = frozenset(
    {
        "csep",
        "gep",
        "ilr",
        "cos",
        "cas",
        "fsw",
        "cec",
        "fst",
        "opt",
        "cpt",
        "f-1",
        "f1",
        "h-1b",
        "h1b",
        "o-1",
        "e-2",
        "l-1",
        "j-1",
        "eb5",
        "485",
        "aewv",
    }
)

# Legacy core aliases preserved cleanly alongside consolidated coverage markers.
_BASE_TEXT_MARKERS: dict[str, tuple[str, ...]] = {
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


# Canonical aliases between pathway_seeds.json, corpus_reliability, and agent evals.
_PATHWAY_CANONICAL_ALIASES: dict[str, str] = {
    "nl_fees_hub": "nl_fees",
    "sg_permanent_resident": "sg_permanent_residence",
    "nz_study_visas": "nz_student_visa",
}


def canonical_pathway_id(pathway_id: str | None) -> str | None:
    cleaned = (pathway_id or "").strip()
    if not cleaned:
        return None
    return _PATHWAY_CANONICAL_ALIASES.get(cleaned, cleaned)


@lru_cache(maxsize=1)
def _seed_url_tokens() -> dict[str, tuple[str, ...]]:
    """Load unambiguous match_urls from config/coverage/pathway_seeds.json."""
    if not _PATHWAY_SEEDS_PATH.is_file():
        return {}
    try:
        data = json.loads(_PATHWAY_SEEDS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for entry in data.get("pathways") or []:
        if not isinstance(entry, dict):
            continue
        pid = canonical_pathway_id(str(entry.get("pathway_id") or "").strip())
        if not pid:
            continue
        urls = [
            str(u).strip().lower()
            for u in (entry.get("match_urls") or [])
            if isinstance(u, str)
            and len(u.strip()) >= 6
            and u.strip().lower() not in _IGNORED_SEED_URL_TOKENS
        ]
        if urls:
            out[pid] = tuple(urls)
    return out


@lru_cache(maxsize=1)
def pathway_url_token_map() -> dict[str, tuple[str, ...]]:
    merged: dict[str, list[str]] = {}
    for src_map in (
        PATHWAY_URL_TOKENS,
        _seed_url_tokens(),
        {
            "ie_critical_skills_employment_permit": (
                "/critical-skills",
                "critical-skills-employment",
            ),
            "ie_general_employment_permit": (
                "/general-employment",
                "general-employment-permit",
            ),
            "gb_skilled_worker": ("/skilled-worker-visa",),
            "gb_student": ("/student-visa",),
        },
    ):
        for raw_pid, tokens in src_map.items():
            pid = canonical_pathway_id(raw_pid) or raw_pid
            bucket = merged.setdefault(pid, [])
            for tok in tokens:
                t = tok.strip().lower()
                if t and t not in bucket:
                    bucket.append(t)
    return {pid: tuple(toks) for pid, toks in merged.items()}


@lru_cache(maxsize=1)
def pathway_marker_map() -> dict[str, tuple[str, ...]]:
    merged: dict[str, list[str]] = {}
    for pid, markers in _BASE_TEXT_MARKERS.items():
        merged[pid] = list(markers)
    for raw_pid, markers in PATHWAY_BODY_MARKERS.items():
        pid = canonical_pathway_id(raw_pid) or raw_pid
        bucket = merged.setdefault(pid, [])
        for m in markers:
            ml = m.strip().lower()
            # Keep ambiguous generic single words out of global detection.
            if ml in {"student", "work", "residence", "citizenship", "485"}:
                continue
            if ml and ml not in bucket:
                bucket.append(ml)
    return {pid: tuple(toks) for pid, toks in merged.items()}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _marker_in_text(marker: str, blob: str) -> bool:
    m = marker.strip().lower()
    if not m:
        return False
    if m in _BOUNDARY_ONLY_MARKERS or (len(m) <= 4 and m.isalnum()):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(m)}(?![a-z0-9])", blob))
    return m in blob


def _country_prefix(pathway_id: str | None) -> str | None:
    if not pathway_id or "_" not in pathway_id:
        return None
    return pathway_id.split("_", 1)[0].lower()


def has_explicit_comparison_language(text: str) -> bool:
    """True when the claim uses explicit cross-pathway comparison wording."""
    return bool(_COMPARISON_RE.search(text or ""))


def detect_pathway_ids(
    text: str,
    *,
    requested_pathway_id: str | None = None,
) -> list[str]:
    """Return pathway IDs whose markers appear in text (longest-marker preference).

    When ``requested_pathway_id`` is provided, restricts cross-pathway text-marker
    checks to pathways sharing the same country prefix (plus core multi-word
    aliases) so e.g. 'student visa' in UK does not falsely match 'nz_student_visa'.
    """
    blob = _normalize(text)
    if not blob:
        return []
    req_country = _country_prefix(requested_pathway_id)
    found: list[str] = []
    for pathway_id, markers in pathway_marker_map().items():
        if req_country and pathway_id not in _BASE_TEXT_MARKERS:
            if _country_prefix(pathway_id) != req_country:
                continue
        if (
            req_country
            and pathway_id in _BASE_TEXT_MARKERS
            and _country_prefix(pathway_id) != req_country
            and pathway_id == "gb_student"
        ):
            # Avoid attributing 'student visa' text inside AU/NZ/US requests to GB.
            continue
        ordered = sorted(markers, key=len, reverse=True)
        if any(_marker_in_text(m, blob) for m in ordered):
            found.append(pathway_id)
    return found


def pathway_ids_from_url(url: str | None) -> list[str]:
    raw = (url or "").strip().lower()
    if not raw:
        return []
    norm_key = url_key(raw)
    hits: list[str] = []
    for pathway_id, frags in pathway_url_token_map().items():
        exclusions = PATHWAY_HARD_EXCLUSIONS.get(pathway_id, ())
        if any(ex and ex.lower() in raw for ex in exclusions):
            continue
        if any((f in norm_key or f in raw) for f in frags):
            hits.append(pathway_id)
    return hits


def evidence_pathway_signals(
    item: EvidenceItem,
    *,
    requested_pathway_id: str | None = None,
) -> list[str]:
    """Pathway signals for an evidence item.

    Curated LegalRule records carry authoritative pathway_id provenance when set.
    Otherwise, when the official URL clearly identifies a pathway page, URL signals
    win so cross-mentions (e.g. GEP text referencing the Critical Skills list)
    cannot re-attribute that page's figures.
    """
    if item.source_type == EvidenceSourceType.CURATED_RULE:
        rule_pid = (item.provenance or {}).get("pathway_id")
        if isinstance(rule_pid, str) and rule_pid.strip():
            return [canonical_pathway_id(rule_pid) or rule_pid.strip()]

    url_hits = pathway_ids_from_url(item.source_url)
    if url_hits:
        return url_hits
    parts = [
        item.passage_text or "",
        item.heading_path or "",
        item.source_url or "",
        item.title or str((item.provenance or {}).get("title") or ""),
    ]
    return detect_pathway_ids(
        " ".join(parts),
        requested_pathway_id=canonical_pathway_id(requested_pathway_id),
    )


def _hard_exclusion_hit(item: EvidenceItem, pathway_id: str) -> str | None:
    canon = canonical_pathway_id(pathway_id) or pathway_id
    exclusions = PATHWAY_HARD_EXCLUSIONS.get(canon) or PATHWAY_HARD_EXCLUSIONS.get(pathway_id) or ()
    if not exclusions:
        return None
    blob = " ".join(
        [
            url_key(item.source_url or ""),
            (item.source_url or "").lower(),
            (item.title or str((item.provenance or {}).get("title") or "")).lower(),
        ]
    )
    for ex in exclusions:
        if ex and ex.lower() in blob:
            return ex
    return None


def normalize_numeric_literal(value: str) -> str:
    """Locale-aware canonical normalization preserving decimal commas and ambiguity."""
    return format_canonical_text_literals(value or "")


def numeric_literals_in_text(text: str) -> list[str]:
    return [
        q.raw_literal
        for q in extract_numeric_quantities(
            text or "", include_years=False, include_bare_integers=True
        )
    ]


def passage_contains_literal(passage: str, literal: str) -> bool:
    q = parse_single_quantity(literal or "")
    if q is None:
        return False
    return diagnose_claim_numeric_support(
        literal or "", [passage or ""], include_years=q.is_year_like
    ).supported


def number_supported_by_requested_pathway_passage(
    *,
    claim: str,
    cited: list[EvidenceItem],
    pathway_id: str,
) -> bool:
    """True when each claim numeric appears in a passage that marks the requested pathway.

    Uses Decimal, currency, and unit/period matching so an amount on another pathway
    (even with matching digits or different period/currency) cannot satisfy locality.
    """
    claim_qs = extract_numeric_quantities(
        claim or "", include_years=False, include_bare_integers=True
    )
    if not claim_qs:
        return True
    requested = canonical_pathway_id(pathway_id)
    if not requested:
        return True
    for cq in claim_qs:
        passages_with_lit = [
            item
            for item in cited
            if any(
                quantity_matches_evidence(cq, eq).matched
                for eq in quantities_from_evidence_item(item)
            )
        ]
        if not passages_with_lit:
            # Literal absent/mismatched in citations — handled by numeric grounding elsewhere.
            continue
        # Locality applies only when passages that carry the figure also carry
        # pathway signals. Unmarked synthetic/partial excerpts must not be treated
        # as cross-pathway mismatches (false-positive suppression).
        signaled = [
            item
            for item in passages_with_lit
            if evidence_pathway_signals(item, requested_pathway_id=requested)
            or _hard_exclusion_hit(item, requested)
        ]
        if not signaled:
            continue
        if not any(
            requested
            in evidence_pathway_signals(item, requested_pathway_id=requested)
            and not _hard_exclusion_hit(item, requested)
            for item in signaled
        ):
            return False
    return True


def classify_pathway_applicability(
    *,
    claim: str,
    cited: list[EvidenceItem],
    pathway_id: str | None,
) -> PathwayApplicabilityResult:
    """Classify whether cited evidence can support a pathway-scoped claim."""
    requested = canonical_pathway_id(pathway_id)
    if not requested:
        return PathwayApplicabilityResult(
            classification=PathwayApplicability.NOT_APPLICABLE,
            reason="no_pathway_hint",
            requested_pathway_id=None,
            pathways_in_claim=tuple(),
            pathways_in_evidence=tuple(),
        )

    claim_pathways = tuple(
        detect_pathway_ids(claim, requested_pathway_id=requested)
    )
    evidence_pathways: list[str] = []
    excluded_hits: list[str] = []
    for item in cited:
        ex = _hard_exclusion_hit(item, requested)
        if ex:
            excluded_hits.append(ex)
        for pid in evidence_pathway_signals(item, requested_pathway_id=requested):
            if pid not in evidence_pathways:
                evidence_pathways.append(pid)
    evidence_tuple = tuple(evidence_pathways)

    has_number = bool(numeric_literals_in_text(claim or ""))
    comparison = has_explicit_comparison_language(claim)

    if comparison and (
        requested in claim_pathways
        or requested in evidence_tuple
        or any(p != requested for p in claim_pathways)
    ):
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

    # Hard exclusion when evidence belongs to an excluded pathway/portal for requested pathway.
    if excluded_hits and requested not in evidence_tuple:
        if has_number or (requested in claim_pathways):
            return PathwayApplicabilityResult(
                classification=PathwayApplicability.CROSS_PATHWAY,
                reason=f"hard_exclusion:{excluded_hits[0]}",
                requested_pathway_id=requested,
                pathways_in_claim=claim_pathways,
                pathways_in_evidence=evidence_tuple,
            )

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

    if requested in evidence_tuple:
        return PathwayApplicabilityResult(
            classification=PathwayApplicability.DIRECT,
            reason="evidence_mentions_requested_pathway",
            requested_pathway_id=requested,
            pathways_in_claim=claim_pathways,
            pathways_in_evidence=evidence_tuple,
        )

    if has_number and evidence_tuple and requested not in evidence_tuple:
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
    del claim
    if result.requested_pathway_id is None:
        return False
    return result.classification == PathwayApplicability.CROSS_PATHWAY
