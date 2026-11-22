"""Deterministic qualifier, exception, and conditional claim verification (Phase 10C.3).

Bounded recognition for immigration-related conditions, polarities, and
cross-passage compatibility. Does not attempt unrestricted natural-language
entailment and introduces no LLM calls.

Statuses are automated evidence assessments, not human legal certification.
Unrecognized conditions are surfaced explicitly rather than silently discarded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable
from urllib.parse import urlparse

from lifepilot.agent.evidence import EvidenceItem
from lifepilot.agent.numeric import extract_numeric_quantities
from lifepilot.agent.pathway_applicability import (
    detect_pathway_ids,
    evidence_pathway_signals,
    has_explicit_comparison_language,
)
from lifepilot.agent.temporal_dates import extract_effective_date_mentions

# ---------------------------------------------------------------------------
# Shared cohort / scope patterns (single source of truth for 10C.3 + callers)
# ---------------------------------------------------------------------------

COHORT_SCOPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("transitional", re.compile(r"(?i)\b(transitional|continually\s+held|tier\s*2|legacy\s+cohort|pre-?2024)\b")),
    ("extending_updating", re.compile(r"(?i)\b(extending|updating\s+(?:the\s+)?visa)\b")),
    ("under_26", re.compile(r"(?i)\bunder\s+26\b")),
    ("under_30", re.compile(r"(?i)\bunder\s+30\b")),
    ("over_30", re.compile(r"(?i)\b(30\s+or\s+older|over\s+30)\b")),
    ("new_entrant", re.compile(r"(?i)\b(new\s+entrants?|recent\s+graduates?|professional\s+training)\b")),
    ("phd_stem", re.compile(r"(?i)\b(stem\s+phd|phd\s+in\s+(?:a\s+)?stem)\b")),
    ("phd_non_stem", re.compile(r"(?i)\b(non-?stem|relevant\s+phd)\b")),
    ("postdoc", re.compile(r"(?i)\bpostdoctoral\b")),
    ("healthcare_education", re.compile(r"(?i)\b(healthcare|national\s+pay\s+scale|teaching|prison\s+officer)\b")),
    ("outside_london", re.compile(r"(?i)\boutside\s+london\b")),
    ("inside_london", re.compile(r"(?i)\b(in|inside)\s+london\b")),
    ("financial_sector", re.compile(r"(?i)\bfinancial\s+services\b")),
)

CONDITIONAL_QUALIFIER_RE = re.compile(
    r"(?i)\b("
    r"transitional|in\s+some\s+cases|for\s+those\s+who|unless|except|"
    r"provided\s+that|subject\s+to|reduced\s+rate|"
    r"lower\s+(?:requirements?|going\s+rate|threshold)|"
    r"discount|if\s+(?:you|the\s+applicant|they|an?\s+applicant)|"
    r"apply\s+only\s+to|applies\s+only\s+to|only\s+for\s+|only\s+if\s+|"
    r"only\s+when\s+|only\s+to\s+applications|only\s+before\s+"
    r")\b"
)

_MATERIAL_CONDITIONAL_MARKER_RE = re.compile(
    r"(?i)\b("
    r"only\s+if|only\s+when|only\s+for|only\s+to|"
    r"unless|except(?:ion)?|provided\s+that|subject\s+to|"
    r"if\s+(?:you|the\s+applicant|they|an?\s+applicant)|"
    r"where\s+(?:the\s+)?applicant|for\s+those\s+who|"
    r"in\s+some\s+cases|applies?\s+only\s+to"
    r")\b"
)

_UNIVERSAL_CLAIM_RE = re.compile(
    r"(?i)\b("
    r"all\s+applicants|every\s+applicant|anyone|"
    r"always\s+(?:eligible|exempt|required|permitted|allowed)|"
    r"without\s+exception|no\s+exceptions|"
    r"regardless\s+of|in\s+all\s+cases"
    r")\b"
)

_ELIGIBILITY_OR_REQUIREMENT_ASSERTION_RE = re.compile(
    r"(?i)\b("
    r"eligible|ineligible|qualify|qualifies|qualification|"
    r"required|requirement|must|need(?:s|ed)?|"
    r"exempt|exemption|permitted|prohibited|allowed|"
    r"sponsorship|sponsor|threshold|going\s+rate|"
    r"minimum|maximum|at\s+least|at\s+most"
    r")\b"
)

_HIGH_RISK_LEGAL_ASSERTION_RE = re.compile(
    r"(?i)\b("
    r"eligible|ineligible|must\s+not|must\b|exempt|prohibited|"
    r"do\s+not\s+need|not\s+required|sponsorship|"
    r"all\s+applicants|without\s+exception|guaranteed"
    r")\b"
)


class BoundDirection(str, Enum):
    AT_LEAST = "at_least"
    AT_MOST = "at_most"
    EXACT = "exact"
    UNKNOWN = "unknown"


class TemporalDirection(str, Enum):
    BEFORE = "before"
    AFTER = "after"
    ON_OR_AFTER = "on_or_after"
    ON_OR_BEFORE = "on_or_before"
    UNKNOWN = "unknown"


class GeographicScope(str, Enum):
    INSIDE = "inside"
    OUTSIDE = "outside"
    UNKNOWN = "unknown"


class QualifierDecision(str, Enum):
    PASS = "pass"
    CONTRADICTED = "contradicted"
    INSUFFICIENT = "insufficient"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class BoundConstraint:
    direction: BoundDirection
    number_token: str
    raw_span: str


@dataclass(frozen=True)
class PolaritySignal:
    axis: str
    polarity: str  # positive | negative
    span: str


@dataclass
class QualifierProfile:
    """Structured, bounded recognition of conditions present in text."""

    cohorts: frozenset[str] = frozenset()
    conditionals: frozenset[str] = frozenset()
    polarities: tuple[PolaritySignal, ...] = ()
    bounds: tuple[BoundConstraint, ...] = ()
    temporal_directions: frozenset[str] = frozenset()
    geographic: frozenset[str] = frozenset()
    age_cues: frozenset[str] = frozenset()
    education_cues: frozenset[str] = frozenset()
    occupation_cues: frozenset[str] = frozenset()
    sponsorship_cues: frozenset[str] = frozenset()
    salary_cues: frozenset[str] = frozenset()
    transitional: bool = False
    universal: bool = False
    dates: frozenset[tuple[str, str]] = frozenset()
    unrecognized_condition_markers: frozenset[str] = frozenset()
    is_conditional: bool = False


@dataclass
class MultiPassageAssessment:
    compatible: bool
    reasons: list[str] = field(default_factory=list)
    relationship: str = "unknown"


@dataclass
class QualifierAssessment:
    decision: QualifierDecision
    reasons: list[str] = field(default_factory=list)
    claim_profile: QualifierProfile | None = None
    evidence_profile: QualifierProfile | None = None
    multi_passage: MultiPassageAssessment | None = None


# ---------------------------------------------------------------------------
# Pattern tables
# ---------------------------------------------------------------------------

_POLARITY_AXES: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    (
        "eligibility",
        re.compile(r"(?i)\b(eligible|qualify|qualifies|qualification)\b"),
        re.compile(r"(?i)\b(ineligible|not\s+eligible|do(?:es)?\s+not\s+qualify|disqualified)\b"),
    ),
    (
        "permission",
        re.compile(r"(?i)\b(permitted|allowed|may\s+(?:work|apply)|unrestricted)\b"),
        re.compile(r"(?i)\b(prohibited|forbidden|not\s+permitted|not\s+allowed|banned)\b"),
    ),
    (
        "requirement",
        re.compile(r"(?i)\b(required|must\b|need(?:s|ed)?\b|compulsory|mandatory)\b"),
        re.compile(
            r"(?i)\b("
            r"exempt|exemption|not\s+required|do(?:es)?\s+not\s+need|"
            r"need\s+not|no\s+need(?:\s+for)?|optional"
            r")\b"
        ),
    ),
    (
        "must_modal",
        re.compile(r"(?i)\bmust\b(?!\s+not)"),
        re.compile(r"(?i)\bmust\s+not\b"),
    ),
)

_BOUND_AT_LEAST_RE = re.compile(
    r"(?i)\b(at\s+least|minimum(?:\s+of)?|no\s+less\s+than|not\s+less\s+than|"
    r"or\s+more|and\s+above)\b"
)
_BOUND_AT_MOST_RE = re.compile(
    r"(?i)\b(at\s+most|maximum(?:\s+of)?|no\s+more\s+than|not\s+more\s+than|"
    r"up\s+to|or\s+less|and\s+below|capped\s+at)\b"
)

_TEMPORAL_BEFORE_RE = re.compile(
    r"(?i)\b(before|prior\s+to|earlier\s+than|until(?!\s+further))\b"
)
_TEMPORAL_AFTER_RE = re.compile(
    r"(?i)\b(after|following|later\s+than|from(?=\s+\d)|on\s+or\s+after)\b"
)

_GEO_INSIDE_RE = re.compile(r"(?i)\b(in|inside)\s+london\b|\bwithin\s+(?:the\s+)?(?:uk|united\s+kingdom|eea|eu)\b")
_GEO_OUTSIDE_RE = re.compile(r"(?i)\boutside\s+london\b|\boutside\s+(?:the\s+)?(?:uk|united\s+kingdom|eea|eu)\b")

_AGE_RE = re.compile(
    r"(?i)\b("
    r"under\s+\d{1,2}|over\s+\d{1,2}|\d{1,2}\s+or\s+older|"
    r"aged?\s+\d{1,2}|age\s+(?:of\s+)?\d{1,2}|between\s+\d{1,2}\s+and\s+\d{1,2}"
    r")\b"
)
_EDUCATION_RE = re.compile(
    r"(?i)\b("
    r"phd|doctoral|degree|bachelor|master(?:'s)?|qualification|"
    r"rqf|nqf|scqf|graduate|postgraduate|a-?level"
    r")\b"
)
_OCCUPATION_RE = re.compile(
    r"(?i)\b("
    r"occupation(?:al)?|soc\s*code|job\s+title|going\s+rate|"
    r"skilled\s+worker|critical\s+skills|healthcare|teaching|"
    r"financial\s+services|code\s+\d{4}"
    r")\b"
)
_SPONSORSHIP_RE = re.compile(
    r"(?i)\b("
    r"sponsor(?:ship|ed)?|certificate\s+of\s+sponsorship|cos\b|"
    r"unrestricted\s+work\s+authorization|work\s+authorization"
    r")\b"
)
_SALARY_RE = re.compile(
    r"(?i)\b("
    r"salary|going\s+rate|threshold|remuneration|pay\s+scale|"
    r"general\s+salary|hourly|annual(?:ly)?|per\s+year|per\s+annum"
    r")\b"
)

# Consequence stripped of its governing condition.
_CONDITIONAL_EXEMPTION_RE = re.compile(
    r"(?i)"
    r"(?P<consequent>"
    r"(?:do(?:es)?\s+not\s+need|need\s+not|are\s+not\s+required\s+to|"
    r"is\s+not\s+required\s+to|exempt\s+from|no\s+need\s+(?:for|to))\s+"
    r"[^.]{3,80}?"
    r")"
    r"\s+(?:if|unless|provided\s+that|subject\s+to|only\s+if|when|where)\s+"
    r"(?P<condition>[^.]{3,120})"
)

_REQUIREMENT_WITH_EXCEPTION_RE = re.compile(
    r"(?i)"
    r"(?P<requirement>"
    r"[^.]{0,100}?\b(?:must|is\s+required|are\s+required|required|need(?:s)?\s+to|requires?)\b[^.]{0,40}?"
    r")"
    r"\s+(?:unless|except(?:\s+when|\s+if)?|provided\s+that|subject\s+to)\s+"
    r"(?P<exception>[^.]{3,120})"
)

_ONLY_IF_RE = re.compile(
    r"(?i)"
    r"(?P<head>[^.]{3,80}?)\s+"
    r"(?:only\s+if|only\s+when|only\s+where|only\s+for)\s+"
    r"(?P<condition>[^.]{3,120})"
)

_UNRECOGNIZED_CONDITION_HINT_RE = re.compile(
    r"(?i)\b("
    r"depending\s+on|depends\s+on|in\s+accordance\s+with|"
    r"as\s+set\s+out|see\s+(?:table|appendix|schedule)|"
    r"further\s+conditions?\s+apply|other\s+conditions?\s+apply|"
    r"criteria\s+in\s+(?:the\s+)?(?:guidance|rules)"
    r")\b"
)

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _url_host(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""


def _normalize_span(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _bound_direction_near(text: str, start: int, end: int) -> BoundDirection:
    window = (text or "")[max(0, start - 40) : min(len(text or ""), end + 40)]
    has_least = bool(_BOUND_AT_LEAST_RE.search(window))
    has_most = bool(_BOUND_AT_MOST_RE.search(window))
    if has_least and not has_most:
        return BoundDirection.AT_LEAST
    if has_most and not has_least:
        return BoundDirection.AT_MOST
    if has_least and has_most:
        return BoundDirection.UNKNOWN
    return BoundDirection.EXACT


def extract_bound_constraints(text: str) -> tuple[BoundConstraint, ...]:
    """Pair numeric literals with local at-least / at-most cues."""
    blob = text or ""
    out: list[BoundConstraint] = []
    for q in extract_numeric_quantities(blob, include_years=False, include_bare_integers=True):
        direction = _bound_direction_near(blob, q.start_char, q.end_char)
        if direction in {BoundDirection.AT_LEAST, BoundDirection.AT_MOST}:
            span = blob[max(0, q.start_char - 40) : min(len(blob), q.end_char + 10)]
            out.append(
                BoundConstraint(
                    direction=direction,
                    number_token=q.number_token,
                    raw_span=_normalize_span(span),
                )
            )
    return tuple(out)


def _extract_polarities(text: str) -> tuple[PolaritySignal, ...]:
    blob = text or ""
    signals: list[PolaritySignal] = []
    for axis, pos_re, neg_re in _POLARITY_AXES:
        for m in pos_re.finditer(blob):
            # Skip "must" that is actually "must not"
            if axis == "must_modal" and re.match(r"(?i)^must\s+not\b", blob[m.start() : m.start() + 12]):
                continue
            # Skip positive requirement matches inside clear exemption phrasing.
            if axis == "requirement" and re.search(
                r"(?i)\b(not\s+required|do(?:es)?\s+not\s+need|exempt)\b",
                blob[max(0, m.start() - 20) : m.end() + 20],
            ):
                continue
            signals.append(PolaritySignal(axis=axis, polarity="positive", span=m.group(0).lower()))
        for m in neg_re.finditer(blob):
            signals.append(PolaritySignal(axis=axis, polarity="negative", span=m.group(0).lower()))
    return tuple(signals)


def _extract_conditionals(text: str) -> frozenset[str]:
    found: set[str] = set()
    for m in _MATERIAL_CONDITIONAL_MARKER_RE.finditer(text or ""):
        found.add(_normalize_span(m.group(0)))
    return frozenset(found)


def extract_qualifier_profile(text: str) -> QualifierProfile:
    """Deterministically recognize common immigration qualifiers in text."""
    blob = text or ""
    cohorts = frozenset(name for name, pat in COHORT_SCOPE_PATTERNS if pat.search(blob))
    conditionals = _extract_conditionals(blob)
    polarities = _extract_polarities(blob)
    bounds = extract_bound_constraints(blob)

    temporal: set[str] = set()
    if _TEMPORAL_BEFORE_RE.search(blob):
        temporal.add(TemporalDirection.BEFORE.value)
    if _TEMPORAL_AFTER_RE.search(blob):
        temporal.add(TemporalDirection.AFTER.value)

    geographic: set[str] = set()
    if _GEO_INSIDE_RE.search(blob):
        geographic.add(GeographicScope.INSIDE.value)
    if _GEO_OUTSIDE_RE.search(blob):
        geographic.add(GeographicScope.OUTSIDE.value)

    age = frozenset(_normalize_span(m.group(0)) for m in _AGE_RE.finditer(blob))
    education = frozenset(_normalize_span(m.group(0)) for m in _EDUCATION_RE.finditer(blob))
    occupation = frozenset(_normalize_span(m.group(0)) for m in _OCCUPATION_RE.finditer(blob))
    sponsorship = frozenset(_normalize_span(m.group(0)) for m in _SPONSORSHIP_RE.finditer(blob))
    salary = frozenset(_normalize_span(m.group(0)) for m in _SALARY_RE.finditer(blob))

    mentions = extract_effective_date_mentions(blob)
    dates = frozenset(
        (m.role, m.parsed_date.isoformat() if m.parsed_date else m.date_literal)
        for m in mentions
    )

    unrecognized = frozenset(
        _normalize_span(m.group(0)) for m in _UNRECOGNIZED_CONDITION_HINT_RE.finditer(blob)
    )

    transitional = "transitional" in cohorts or bool(
        re.search(r"(?i)\btransitional\b", blob)
    )
    universal = bool(_UNIVERSAL_CLAIM_RE.search(blob))
    is_conditional = bool(
        cohorts
        or conditionals
        or dates
        or CONDITIONAL_QUALIFIER_RE.search(blob)
        or unrecognized
    )

    return QualifierProfile(
        cohorts=cohorts,
        conditionals=conditionals,
        polarities=polarities,
        bounds=bounds,
        temporal_directions=frozenset(temporal),
        geographic=frozenset(geographic),
        age_cues=age,
        education_cues=education,
        occupation_cues=occupation,
        sponsorship_cues=sponsorship,
        salary_cues=salary,
        transitional=transitional,
        universal=universal,
        dates=dates,
        unrecognized_condition_markers=unrecognized,
        is_conditional=is_conditional,
    )


# ---------------------------------------------------------------------------
# Conflict / entailment checks
# ---------------------------------------------------------------------------

def _axis_polarities(profile: QualifierProfile, axis: str) -> set[str]:
    return {p.polarity for p in profile.polarities if p.axis == axis}


def detect_polarity_conflicts(
    claim_profile: QualifierProfile,
    evidence_profile: QualifierProfile,
) -> list[str]:
    """Opposite legal meanings on the same axis (eligible vs ineligible, etc.)."""
    reasons: list[str] = []
    for axis, _, _ in _POLARITY_AXES:
        claim_pols = _axis_polarities(claim_profile, axis)
        ev_pols = _axis_polarities(evidence_profile, axis)
        if not claim_pols or not ev_pols:
            continue
        # Conflict when claim asserts one polarity and evidence asserts only the opposite,
        # or both assert opposite without claim acknowledging both.
        if claim_pols == {"positive"} and ev_pols == {"negative"}:
            reasons.append(f"polarity_conflict:{axis}:claim_positive_evidence_negative")
        elif claim_pols == {"negative"} and ev_pols == {"positive"}:
            # Claim may correctly negate a positive requirement when it retains conditions;
            # only flag hard antonyms when claim has no conditional framing and evidence
            # does not itself contain the negative polarity.
            if not claim_profile.is_conditional and "negative" not in ev_pols:
                reasons.append(f"polarity_conflict:{axis}:claim_negative_evidence_positive")
        elif claim_pols == {"positive"} and "negative" in ev_pols and "positive" not in ev_pols:
            reasons.append(f"polarity_conflict:{axis}:claim_positive_evidence_negative")
    return reasons


def detect_bound_direction_conflicts(
    claim_profile: QualifierProfile,
    evidence_profile: QualifierProfile,
) -> list[str]:
    """At-least vs at-most (and similar) inversions for the same numeric token."""
    reasons: list[str] = []
    ev_by_num: dict[str, set[BoundDirection]] = {}
    for b in evidence_profile.bounds:
        ev_by_num.setdefault(b.number_token, set()).add(b.direction)
    for b in claim_profile.bounds:
        ev_dirs = ev_by_num.get(b.number_token) or set()
        if not ev_dirs:
            continue
        if b.direction == BoundDirection.AT_LEAST and BoundDirection.AT_MOST in ev_dirs:
            if BoundDirection.AT_LEAST not in ev_dirs:
                reasons.append(
                    f"bound_direction_conflict:at_least_vs_at_most:{b.number_token}"
                )
        elif b.direction == BoundDirection.AT_MOST and BoundDirection.AT_LEAST in ev_dirs:
            if BoundDirection.AT_MOST not in ev_dirs:
                reasons.append(
                    f"bound_direction_conflict:at_most_vs_at_least:{b.number_token}"
                )
    return reasons


def detect_temporal_direction_conflicts(
    claim_profile: QualifierProfile,
    evidence_profile: QualifierProfile,
) -> list[str]:
    reasons: list[str] = []
    c = claim_profile.temporal_directions
    e = evidence_profile.temporal_directions
    if TemporalDirection.BEFORE.value in c and TemporalDirection.AFTER.value in e:
        if TemporalDirection.BEFORE.value not in e:
            reasons.append("temporal_direction_conflict:before_vs_after")
    if TemporalDirection.AFTER.value in c and TemporalDirection.BEFORE.value in e:
        if TemporalDirection.AFTER.value not in e:
            reasons.append("temporal_direction_conflict:after_vs_before")
    return reasons


def detect_geographic_conflicts(
    claim_profile: QualifierProfile,
    evidence_profile: QualifierProfile,
) -> list[str]:
    reasons: list[str] = []
    c = claim_profile.geographic
    e = evidence_profile.geographic
    if GeographicScope.INSIDE.value in c and GeographicScope.OUTSIDE.value in e:
        if GeographicScope.INSIDE.value not in e:
            reasons.append("geographic_scope_conflict:inside_vs_outside")
    if GeographicScope.OUTSIDE.value in c and GeographicScope.INSIDE.value in e:
        if GeographicScope.OUTSIDE.value not in e:
            reasons.append("geographic_scope_conflict:outside_vs_inside")
    # Cohort geography pairs (inside_london / outside_london)
    if "inside_london" in claim_profile.cohorts and "outside_london" in evidence_profile.cohorts:
        if "inside_london" not in evidence_profile.cohorts:
            reasons.append("geographic_scope_conflict:inside_london_vs_outside_london")
    if "outside_london" in claim_profile.cohorts and "inside_london" in evidence_profile.cohorts:
        if "outside_london" not in evidence_profile.cohorts:
            reasons.append("geographic_scope_conflict:outside_london_vs_inside_london")
    return reasons


def _consequent_supported_without_condition(
    claim: str,
    consequent: str,
    condition: str,
) -> bool:
    """True when claim restates the consequent but drops the governing condition."""
    claim_l = _normalize_span(claim)
    cons_tokens = _tokens(consequent)
    cond_tokens = _tokens(condition)
    claim_tokens = _tokens(claim)
    if len(cons_tokens) < 2:
        return False
    overlap = cons_tokens & claim_tokens
    if len(overlap) < max(2, int(0.5 * len(cons_tokens))):
        return False
    # Condition retained if material condition tokens or conditional markers appear.
    if claim_profile_has_condition_retention(claim, cond_tokens):
        return False
    return True


def claim_profile_has_condition_retention(claim: str, cond_tokens: set[str]) -> bool:
    claim_l = claim or ""
    if _MATERIAL_CONDITIONAL_MARKER_RE.search(claim_l) or CONDITIONAL_QUALIFIER_RE.search(claim_l):
        # Soft retention: conditional marker present is usually enough for paraphrases.
        claim_tokens = _tokens(claim_l)
        if not cond_tokens:
            return True
        # Prefer some lexical retention of the condition itself when available.
        if len(cond_tokens & claim_tokens) >= max(1, min(2, len(cond_tokens) // 2)):
            return True
        # Marker alone still counts for short paraphrases ("only if authorised").
        return True
    claim_tokens = _tokens(claim_l)
    return len(cond_tokens & claim_tokens) >= max(2, min(3, len(cond_tokens) // 2))


def detect_stripped_conditional_conclusions(
    claim: str,
    evidence_text: str,
) -> list[str]:
    """Catch conclusions that drop 'if/unless/only if' conditions from evidence."""
    reasons: list[str] = []
    blob = evidence_text or ""
    claim_l = claim or ""

    for m in _CONDITIONAL_EXEMPTION_RE.finditer(blob):
        consequent = m.group("consequent")
        condition = m.group("condition")
        if _consequent_supported_without_condition(claim_l, consequent, condition):
            reasons.append("negation_scope_condition_stripped:conditional_exemption")
            break

    for m in _REQUIREMENT_WITH_EXCEPTION_RE.finditer(blob):
        requirement = m.group("requirement")
        exception = m.group("exception")
        if _consequent_supported_without_condition(claim_l, requirement, exception):
            reasons.append("mandatory_condition_omitted_from_claim:exception_stripped")
            break

    for m in _ONLY_IF_RE.finditer(blob):
        head = m.group("head")
        condition = m.group("condition")
        # Focus on the trailing clause of the head (last ~12 tokens).
        head_tokens = list(_tokens(head))
        focus = " ".join(head_tokens[-12:]) if head_tokens else head
        if _consequent_supported_without_condition(claim_l, focus, condition):
            reasons.append("mandatory_condition_omitted_from_claim:only_if_stripped")
            break

    return reasons


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text or "")
    return [p.strip() for p in parts if p and p.strip()]


def supporting_evidence_spans(claim: str, evidence_text: str) -> list[str]:
    """Return evidence sentences that appear to support the claim's numbers/key tokens.

    Used to avoid requiring unrelated transitional/exception clauses from the same
    passage when the claim correctly restates a distinct general rule.
    """
    blob = evidence_text or ""
    sentences = _split_sentences(blob)
    if not sentences:
        return [blob] if blob.strip() else []

    claim_qs = extract_numeric_quantities(
        claim or "", include_years=False, include_bare_integers=True
    )
    claim_nums = {q.number_token for q in claim_qs if q.number_token}
    supporting: list[str] = []
    if claim_nums:
        for sent in sentences:
            sent_nums = {
                q.number_token
                for q in extract_numeric_quantities(
                    sent, include_years=False, include_bare_integers=True
                )
                if q.number_token
            }
            if claim_nums & sent_nums:
                supporting.append(sent)
    if supporting:
        return supporting

    # Non-numeric: prefer sentences with strong lexical overlap.
    claim_tokens = _tokens(claim)
    if not claim_tokens:
        return sentences
    ranked: list[tuple[float, str]] = []
    for sent in sentences:
        st = _tokens(sent)
        if not st:
            continue
        overlap = claim_tokens & st
        ratio = len(overlap) / max(1, len(claim_tokens))
        if len(overlap) >= 3 and ratio >= 0.28:
            ranked.append((ratio, sent))
    if ranked:
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in ranked[:2]]
    return sentences


def detect_missing_mandatory_qualifiers(
    claim: str,
    claim_profile: QualifierProfile,
    evidence_profile: QualifierProfile,
    *,
    evidence_text: str = "",
) -> list[str]:
    """Require material evidence conditions to be retained on strong claim assertions.

    Conditions are evaluated against the *supporting* evidence spans for the claim
    when possible, so a general threshold claim is not forced to inherit unrelated
    transitional exceptions that govern a different figure in the same passage.
    """
    reasons: list[str] = []
    if not _ELIGIBILITY_OR_REQUIREMENT_ASSERTION_RE.search(claim or ""):
        return reasons

    supporting = supporting_evidence_spans(claim, evidence_text) if evidence_text else []
    support_profile = (
        extract_qualifier_profile(" ".join(supporting))
        if supporting
        else evidence_profile
    )

    # Universal claim against restricted / conditional evidence (whole passage).
    if claim_profile.universal and (
        evidence_profile.is_conditional
        or evidence_profile.cohorts
        or evidence_profile.conditionals
        or evidence_profile.unrecognized_condition_markers
    ):
        reasons.append("unsupported_universal_claim")

    # Supporting spans are conditional; claim asserts without retaining any.
    if support_profile.conditionals and not claim_profile.conditionals:
        if not claim_profile.cohorts and not claim_profile.is_conditional:
            reasons.append("mandatory_condition_omitted_from_claim:evidence_conditional")

    # Cohort on the supporting span omitted from a general-rule claim.
    omitted_cohorts = support_profile.cohorts - claim_profile.cohorts
    if omitted_cohorts and (
        claim_profile.universal
        or (
            not claim_profile.is_conditional
            and bool(
                re.search(
                    r"(?i)\b(the\s+threshold|the\s+requirement|applicants\s+must|must\s+earn)\b",
                    claim or "",
                )
            )
        )
    ):
        if not claim_profile.cohorts and not claim_profile.conditionals:
            if support_profile.cohorts and not claim_profile.transitional:
                # Do not flag when the claim explicitly marks a general/non-cohort rule
                # and the omitted cohorts live only in non-supporting spans.
                if not re.search(r"(?i)\bgeneral\b", claim or ""):
                    reasons.append(
                        "cohort_qualifier_omitted_from_claim:"
                        + ",".join(sorted(omitted_cohorts)[:4])
                    )

    # Age restriction on supporting spans omitted from age-sensitive universal claims.
    if support_profile.age_cues and not claim_profile.age_cues:
        if re.search(r"(?i)\b(must|required|eligible|threshold|salary)\b", claim or ""):
            if not claim_profile.is_conditional and (
                claim_profile.universal
                or re.search(
                    r"(?i)\b(all\s+applicants|applicants\s+must|the\s+minimum)\b",
                    claim or "",
                )
            ):
                reasons.append("age_restriction_omitted_from_claim")

    if evidence_profile.unrecognized_condition_markers and claim_profile.universal:
        reasons.append("unrecognized_evidence_conditions_block_universal_claim")

    return reasons


def assess_multi_passage_compatibility(
    claim: str,
    cited: list[EvidenceItem],
) -> MultiPassageAssessment:
    """Prevent unrelated passages from jointly inventing a new legal conclusion."""
    if len(cited) <= 1:
        return MultiPassageAssessment(compatible=True, relationship="single_passage")

    reasons: list[str] = []
    claim_profile = extract_qualifier_profile(claim)
    cohorts = [extract_qualifier_profile(c.passage_text or "").cohorts for c in cited]
    non_empty_cohorts = [c for c in cohorts if c]
    if len(non_empty_cohorts) >= 2:
        shared = set.intersection(*(set(c) for c in non_empty_cohorts))
        # Combining distinct cohort-scoped passages into an unscoped claim is unsafe
        # even when each passage lexically overlaps the claim.
        if not shared and not claim_profile.cohorts:
            reasons.append("cross_passage_distinct_cohorts")

    # If each cited passage independently overlaps the claim lexically AND no
    # distinct-cohort conflict was found, treat as OK.
    claim_tokens = _tokens(claim)
    independent_ok = 0
    for item in cited:
        pt = _tokens(item.passage_text or "")
        overlap = claim_tokens & pt
        ratio = len(overlap) / max(1, len(claim_tokens))
        if len(overlap) >= 4 and ratio >= 0.28:
            independent_ok += 1
    if independent_ok == len(cited) and not reasons:
        return MultiPassageAssessment(
            compatible=True,
            relationship="each_passage_independently_supports_claim",
        )

    version_ids = {
        str(c.document_version_id)
        for c in cited
        if c.document_version_id is not None
    }
    hosts = {_url_host(c.source_url) for c in cited if c.source_url}
    hosts.discard("")
    pathways: set[str] = set()
    for c in cited:
        pathways.update(evidence_pathway_signals(c))
        pathways.update(detect_pathway_ids(c.passage_text or ""))
    claim_paths = set(detect_pathway_ids(claim))

    if len(pathways) >= 2 and not has_explicit_comparison_language(claim):
        # Different pathway markers across citations without comparison language.
        if claim_paths and not claim_paths.issuperset(pathways):
            # Claim doesn't acknowledge the combination.
            if not any(p in (claim or "").lower() for p in pathways):
                reasons.append("cross_passage_pathway_contamination")

    if len(version_ids) >= 2 and len(hosts) >= 2:
        # Distinct documents from distinct hosts combining conditions.
        profiles = [extract_qualifier_profile(c.passage_text or "") for c in cited]
        if any(p.is_conditional for p in profiles) and not extract_qualifier_profile(claim).is_conditional:
            reasons.append("cross_passage_condition_combination_unsupported")

    # Heading-path / char-span relationship within same version.
    if len(version_ids) == 1:
        headings = [(c.heading_path or "").strip() for c in cited]
        if any(headings) and len({h for h in headings if h}) >= 2:
            # Compatible if headings share a common prefix segment.
            parts = [tuple(h.split(">")) for h in headings if h]
            if parts:
                shared_prefix = True
                min_len = min(len(p) for p in parts)
                if min_len == 0:
                    shared_prefix = False
                else:
                    first = parts[0][0].strip().lower()
                    shared_prefix = all(p[0].strip().lower() == first for p in parts)
                if not shared_prefix:
                    # Char proximity is helpful but not sufficient alone; require overlap
                    # or adjacent spans when combining conditionals.
                    spans = [
                        (c.char_start, c.char_end)
                        for c in cited
                        if c.char_start is not None and c.char_end is not None
                    ]
                    adjacent = False
                    if len(spans) >= 2:
                        spans_sorted = sorted(spans, key=lambda s: s[0] or 0)
                        for (a0, a1), (b0, b1) in zip(spans_sorted, spans_sorted[1:]):
                            if a1 is None or b0 is None:
                                continue
                            if b0 <= a1 + 200:
                                adjacent = True
                                break
                    profiles = [extract_qualifier_profile(c.passage_text or "") for c in cited]
                    if any(p.is_conditional for p in profiles) and not adjacent:
                        reasons.append("cross_passage_unrelated_sections")

    # Truncation among multi-passage citations.
    if any(
        getattr(c, "is_truncated", False) or "…[truncated]" in (c.passage_text or "")
        for c in cited
    ):
        if extract_qualifier_profile(claim).universal or _UNIVERSAL_CLAIM_RE.search(claim or ""):
            reasons.append("cross_passage_truncated_blocks_universal_claim")

    if reasons:
        return MultiPassageAssessment(
            compatible=False,
            reasons=reasons,
            relationship="incompatible",
        )
    return MultiPassageAssessment(
        compatible=True,
        relationship="compatible_shared_context",
    )


def assess_truncation_risk(
    claim: str,
    cited: Iterable[EvidenceItem],
    claim_profile: QualifierProfile | None = None,
) -> list[str]:
    """Truncated evidence must not silently justify comprehensive claims."""
    items = list(cited)
    truncated = [
        c
        for c in items
        if getattr(c, "is_truncated", False) or "…[truncated]" in (c.passage_text or "")
    ]
    if not truncated:
        return []
    profile = claim_profile or extract_qualifier_profile(claim)
    reasons: list[str] = []
    if profile.universal or _UNIVERSAL_CLAIM_RE.search(claim or ""):
        reasons.append("cited_excerpt_truncated_qualifiers_may_be_omitted")
    elif _ELIGIBILITY_OR_REQUIREMENT_ASSERTION_RE.search(claim or "") and not profile.is_conditional:
        # Non-exclusive eligibility/requirement claims over truncated text are uncertain.
        reasons.append("truncated_evidence_may_omit_qualifiers")
    elif profile.conditionals:
        # Claim retained some conditionals — still warn lightly via uncertain if exclusivity-like.
        if re.search(r"(?i)\b(all\s+applicants|no\s+exceptions|always)\b", claim or ""):
            reasons.append("cited_excerpt_truncated_qualifiers_may_be_omitted")
    return reasons


def assess_claim_qualifier_entailment(
    claim: str,
    cited: list[EvidenceItem],
) -> QualifierAssessment:
    """Evaluate whether cited evidence establishes the claim's conditions safely.

    Decision policy (bounded, explainable):
    - contradicted: polarity / bound / geographic / temporal inversions
    - insufficient: stripped conditions, unsupported universals, bad multi-passage merges
    - uncertain: truncation may hide qualifiers; opaque unrecognized conditions on strong claims
    - pass: no deterministic conflict detected
    """
    claim_profile = extract_qualifier_profile(claim)
    evidence_blob = " ".join(c.passage_text or "" for c in cited)
    evidence_profile = extract_qualifier_profile(evidence_blob)
    reasons: list[str] = []

    # 1. Hard contradictions
    polar = detect_polarity_conflicts(claim_profile, evidence_profile)
    bounds = detect_bound_direction_conflicts(claim_profile, evidence_profile)
    temporal = detect_temporal_direction_conflicts(claim_profile, evidence_profile)
    geographic = detect_geographic_conflicts(claim_profile, evidence_profile)
    hard = polar + bounds + temporal + geographic
    if hard:
        return QualifierAssessment(
            decision=QualifierDecision.CONTRADICTED,
            reasons=hard,
            claim_profile=claim_profile,
            evidence_profile=evidence_profile,
        )

    # 2. Stripped conditionals / negation scope
    stripped = detect_stripped_conditional_conclusions(claim, evidence_blob)
    missing = detect_missing_mandatory_qualifiers(
        claim,
        claim_profile,
        evidence_profile,
        evidence_text=evidence_blob,
    )
    reasons.extend(stripped)
    reasons.extend(missing)

    # 3. Multi-passage combination
    multi = assess_multi_passage_compatibility(claim, cited)
    if not multi.compatible:
        reasons.extend(multi.reasons)

    # Drop-style insufficient reasons
    insufficient_prefixes = (
        "negation_scope_condition_stripped",
        "mandatory_condition_omitted_from_claim",
        "unsupported_universal_claim",
        "cohort_qualifier_omitted_from_claim",
        "age_restriction_omitted_from_claim",
        "unrecognized_evidence_conditions_block_universal_claim",
        "cross_passage_",
    )
    if any(r.startswith(insufficient_prefixes) for r in reasons):
        return QualifierAssessment(
            decision=QualifierDecision.INSUFFICIENT,
            reasons=reasons,
            claim_profile=claim_profile,
            evidence_profile=evidence_profile,
            multi_passage=multi,
        )

    # 4. Truncation uncertainty
    trunc_reasons = assess_truncation_risk(claim, cited, claim_profile)
    if trunc_reasons:
        return QualifierAssessment(
            decision=QualifierDecision.UNCERTAIN,
            reasons=trunc_reasons,
            claim_profile=claim_profile,
            evidence_profile=evidence_profile,
            multi_passage=multi,
        )

    # Opaque conditions on strong non-conditional claims → uncertain, not silent pass.
    if (
        evidence_profile.unrecognized_condition_markers
        and _ELIGIBILITY_OR_REQUIREMENT_ASSERTION_RE.search(claim or "")
        and not claim_profile.is_conditional
        and not claim_profile.cohorts
    ):
        return QualifierAssessment(
            decision=QualifierDecision.UNCERTAIN,
            reasons=["unrecognized_evidence_conditions_present"],
            claim_profile=claim_profile,
            evidence_profile=evidence_profile,
            multi_passage=multi,
        )

    return QualifierAssessment(
        decision=QualifierDecision.PASS,
        reasons=reasons,
        claim_profile=claim_profile,
        evidence_profile=evidence_profile,
        multi_passage=multi,
    )


def claim_has_high_risk_legal_assertion(claim: str) -> bool:
    return bool(_HIGH_RISK_LEGAL_ASSERTION_RE.search(claim or ""))


def material_qualifier_cues_in_text(text: str) -> list[str]:
    """Cues that final prose must preserve when present in retained claims."""
    blob = text or ""
    cues: list[str] = []
    for pattern in (
        _MATERIAL_CONDITIONAL_MARKER_RE,
        re.compile(r"(?i)\b(at\s+least|at\s+most|must\s+not|exempt|ineligible|prohibited)\b"),
        re.compile(r"(?i)\b(under\s+\d+|over\s+\d+|new\s+entrant|transitional|outside\s+london|inside\s+london)\b"),
        re.compile(r"(?i)\b(before|after|until|from)\s+\d"),
        re.compile(r"(?i)\b(provided\s+that|subject\s+to|unless|except)\b"),
    ):
        for m in pattern.finditer(blob):
            cue = m.group(0).lower().strip()
            if cue and cue not in cues:
                cues.append(cue)
    return cues


def prose_preserves_material_qualifiers(
    retained_sentences: list[str],
    kept_claims: list[str],
) -> bool:
    """False when rebuilt-worthy material qualifier cues from claims are missing in prose."""
    prose_lower = " ".join(retained_sentences).lower()
    for claim in kept_claims:
        for cue in material_qualifier_cues_in_text(claim):
            if cue not in prose_lower:
                return False
    return True
