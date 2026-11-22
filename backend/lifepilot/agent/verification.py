"""Unified automated legal verification for grounded answers.

Runs after generation+grounding in the main agent graph. The same implementation
is used for production answers and evaluation — tests may record extra metrics
but must not supply a separate verification algorithm.

Statuses are automated evidence assessments, not human legal certification.
No second LLM call by default.

Defense-in-depth: overlap, numeric, and pathway checks are re-applied here even
when generation already ran ``validate_and_ground_answer``. That is intentional —
verification remains the single final gate for prod and eval.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from lifepilot.agent.answer import AnswerClaim
from lifepilot.agent.claim_provenance import (
    ClaimEvidenceRef,
    build_claim_evidence_refs,
    same_underlying_document,
)
from lifepilot.agent.evidence import (
    EvidenceItem,
    EvidenceSourceType,
    EvidenceVerificationState,
)
from lifepilot.agent.grounding import (
    claim_numeric_literals_supported,
    claim_passage_overlap_ok,
    compose_grounded_answer_text,
)
from lifepilot.agent.numeric import (
    diagnose_claim_numeric_support,
    extract_numeric_quantities,
    material_literals_for_scrub,
    quantities_conflict,
    quantities_from_evidence_item,
    quantity_matches_evidence,
)
from lifepilot.agent.pathway_applicability import (
    PathwayApplicability,
    classify_pathway_applicability,
    detect_pathway_ids,
    evidence_pathway_signals,
    has_explicit_comparison_language,
    normalize_numeric_literal,
    numeric_literals_in_text,
    passage_contains_literal,
    should_drop_for_pathway,
)
from lifepilot.agent.provenance import (
    NON_NORMATIVE_AUTHORITY_CATEGORIES,
    SourceAuthorityCategory,
)
from lifepilot.agent.qualifiers import (
    COHORT_SCOPE_PATTERNS,
    CONDITIONAL_QUALIFIER_RE,
    QualifierDecision,
    assess_claim_qualifier_entailment,
    claim_has_high_risk_legal_assertion,
    extract_qualifier_profile,
)
from lifepilot.agent.retrieval_policy import (
    detect_high_risk_categories,
    reconcile_effective_dates,
    review_queue_uncertainty,
)
from lifepilot.agent.temporal_dates import (
    extract_effective_date_mentions,
    parse_unambiguous_date_literal,
)

logger = logging.getLogger(__name__)

_CONFLICT_AMOUNT_RE = re.compile(
    r"(?:£|\$|€)\s?\d[\d,]*(?:\.\d+)?|"
    r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b"
)
_HISTORICAL_RE = re.compile(
    r"(?i)\b("
    r"previously|former(ly)?|before\s+\d{4}|until\s+\d{4}|was\s+(?:£|\$|€)|"
    r"historical|no longer applies|superseded|replaced by"
    r")\b"
)
_FUTURE_RE = re.compile(
    r"(?i)\b("
    r"from\s+\d{1,2}\s+\w+\s+20\d{2}|with effect from|coming into force|"
    r"will apply from|effective from\s+20\d{2}|starting\s+20\d{2}"
    r")\b"
)
_FUTURE_MODAL_RE = re.compile(
    r"(?i)\b("
    r"will\s+(?:apply|be|increase|rise|require|take\s+effect|come\s+into\s+force)|"
    r"coming\s+into\s+force|with\s+effect\s+from"
    r")\b"
)
_ELIGIBILITY_OVERCLAIM_RE = re.compile(
    r"(?i)\b("
    r"fully eligible|guaranteed (approval|eligibility)|"
    r"job offer alone makes .+ eligible|"
    r"salary threshold alone is enough|"
    r"automatically approved|definitely eligible"
    r")\b"
)
# Bare material numbers (points, hours, fees without currency) for drop/scrub.
_MATERIAL_BARE_NUM_RE = re.compile(
    r"(?i)(?<![£$€])\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\b(?!\s*%)"
)
_POLAR_CONFLICTS: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    (
        re.compile(r"(?i)\bunlimited\b"),
        re.compile(r"(?i)\b(limited|maximum|no more than|up to\s+\d+)\b"),
    ),
    (
        re.compile(r"(?i)\b(no\s+(?:cap|limit)|without\s+(?:a\s+)?limit)\b"),
        re.compile(r"(?i)\b(limited|maximum|cap(?:ped)?|no more than)\b"),
    ),
    (
        re.compile(r"(?i)\b(always\s+allowed|unrestricted)\b"),
        re.compile(r"(?i)\b(prohibited|not\s+permitted|restricted|limited)\b"),
    ),
)

# Reason-code prefixes that mean the claim must be dropped (not merely qualified).
_QUALIFIER_DROP_REASON_PREFIXES: tuple[str, ...] = (
    "polarity_conflict:",
    "bound_direction_conflict:",
    "temporal_direction_conflict:",
    "geographic_scope_conflict:",
    "negation_scope_condition_stripped",
    "mandatory_condition_omitted_from_claim",
    "unsupported_universal_claim",
    "cohort_qualifier_omitted_from_claim",
    "age_restriction_omitted_from_claim",
    "unrecognized_evidence_conditions_block_universal_claim",
    "cross_passage_",
)


class ClaimVerificationStatus(str, Enum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    TEMPORAL_UNCERTAINTY = "temporal_uncertainty"
    PATHWAY_MISMATCH = "pathway_mismatch"


class IndependentCorroboration(str, Enum):
    NONE = "none"
    REUSED_RETRIEVAL_DISTINCT_SOURCE = "reused_retrieval_distinct_source"
    SEPARATE_OFFICIAL = "separate_official"
    UNAVAILABLE = "unavailable"
    NOT_REQUIRED = "not_required"


class ClaimVerificationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: ClaimVerificationStatus
    reasons: list[str] = Field(default_factory=list)
    original_uncertainty: str | None = None
    revised_uncertainty: str | None = None
    retained: bool = False
    independent_corroboration: IndependentCorroboration = (
        IndependentCorroboration.NOT_REQUIRED
    )
    # Phase 10C.4 additive provenance (backward compatible defaults).
    corroborating_evidence_id: str | None = None
    evidence_refs: list[ClaimEvidenceRef] = Field(default_factory=list)
    provenance_incomplete: bool = False
    corroboration_rejection_reasons: list[str] = Field(default_factory=list)


class VerificationReport(BaseModel):
    """Structured verification result attached to agent responses / eval rows."""

    model_config = ConfigDict(extra="forbid")

    claim_outcomes: list[ClaimVerificationOutcome] = Field(default_factory=list)
    suppressed_claims: list[str] = Field(default_factory=list)
    qualified_claims: list[str] = Field(default_factory=list)
    remaining_uncertainties: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    verification_ms: float = 0.0
    independent_checks: int = 0
    external_calls: int = 0
    estimated_verification_cost_usd: float = 0.0
    high_risk_categories: list[str] = Field(default_factory=list)


class OfficialCorroborator(Protocol):
    """Optional bounded official fetch for high-risk claims lacking distinct corroboration."""

    def corroborate(
        self,
        *,
        claim: str,
        jurisdiction: str,
        pathway_id: str | None,
        existing_evidence: list[EvidenceItem],
    ) -> list[EvidenceItem]:
        ...


@dataclass
class VerificationResult:
    answer: str | None
    claims: list[AnswerClaim]
    outcomes: list[ClaimVerificationOutcome]
    warnings: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    status_hint: str | None = None
    report: VerificationReport = field(default_factory=VerificationReport)
    pre_verification_claims: list[AnswerClaim] = field(default_factory=list)


def _url_key(url: str | None) -> str:
    if not url:
        return ""
    try:
        parts = urlparse(url)
        return f"{parts.netloc.lower()}{parts.path.rstrip('/').lower()}"
    except Exception:  # noqa: BLE001
        return (url or "").strip().lower()


def _is_high_risk_claim(claim: str, question_categories: list[str]) -> bool:
    cats = set(question_categories or [])
    if cats & {
        "salary_threshold",
        "application_fee",
        "financial_requirement",
        "work_authorization",
        "future_effective",
        "eligibility_requirements",
    }:
        if numeric_literals_in_text(claim) or detect_high_risk_categories(claim):
            return True
    return bool(detect_high_risk_categories(claim))


@dataclass
class CorroborationSearchResult:
    """Result of independent corroboration search (Phase 10C.4 / Q-10)."""

    item: EvidenceItem | None = None
    rejection_reasons: list[str] = field(default_factory=list)


def find_distinct_corroboration(
    *,
    claim: str,
    cited: list[EvidenceItem],
    all_evidence: list[EvidenceItem],
    pathway_id: str | None,
) -> EvidenceItem | None:
    """Pick an independent corroborating passage, or None.

    Backward-compatible wrapper around :func:`search_distinct_corroboration`.
    """
    return search_distinct_corroboration(
        claim=claim,
        cited=cited,
        all_evidence=all_evidence,
        pathway_id=pathway_id,
    ).item


def search_distinct_corroboration(
    *,
    claim: str,
    cited: list[EvidenceItem],
    all_evidence: list[EvidenceItem],
    pathway_id: str | None,
) -> CorroborationSearchResult:
    """Find independent corroboration with qualifier and document-scope checks.

    A candidate must share compatible pathway, jurisdiction, numeric value /
    currency / unit, cohort and mandatory conditions (via qualifier entailment),
    authority/legal status, and true source independence. Same underlying
    document or mirrored copies do not count.
    """
    literals = numeric_literals_in_text(claim)
    if not literals:
        return CorroborationSearchResult(
            rejection_reasons=["corroboration_skipped_no_material_numeric"]
        )

    cited_urls = {_url_key(c.source_url) for c in cited}
    cited_ids = {c.evidence_id for c in cited}
    requested = (pathway_id or "").strip() or None
    claim_profile = extract_qualifier_profile(claim)
    claim_jurs = {(c.jurisdiction or "").upper() for c in cited if c.jurisdiction}
    rejections: list[str] = []

    for item in all_evidence:
        key = _url_key(item.source_url)
        if item.evidence_id in cited_ids:
            continue
        if not key:
            rejections.append(f"corroboration_rejected:{item.evidence_id}:missing_url")
            continue
        if key in cited_urls:
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:same_url_key"
            )
            continue
        if any(same_underlying_document(item, c) for c in cited):
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:same_underlying_document"
            )
            continue
        if getattr(item, "source_authority_category", "unknown") in NON_NORMATIVE_AUTHORITY_CATEGORIES:
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:inadequate_authority"
            )
            continue
        legal_status = (item.legal_status or "").strip().lower()
        if legal_status in {
            "proposed",
            "proposed_not_effective",
            "proposed_or_preparatory",
            "withdrawn",
            "dataset_catalog",
            "portal_html_only",
        }:
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:non_enacted_or_portal_status"
            )
            continue
        # Affirmative independent confirmation requires a clear enacted-like status.
        # Unclear/unknown status may remain as context but must not raise confidence.
        if legal_status in {"", "unknown"}:
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:unclear_legal_status"
            )
            continue
        if getattr(item, "is_truncated", False) or "…[truncated]" in (
            item.passage_text or ""
        ):
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:truncated_evidence"
            )
            continue
        if requested and not requested.startswith("eu_") and bool(
            (item.provenance or {}).get("eu_level_source")
        ):
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:eu_level_for_national_pathway"
            )
            continue
        if claim_jurs and (item.jurisdiction or "").upper() not in claim_jurs:
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:jurisdiction_mismatch"
            )
            continue

        num_diag = diagnose_claim_numeric_support(claim, [item])
        if not num_diag.supported:
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:numeric_incompatible:"
                f"{num_diag.reason}"
            )
            continue
        if requested and requested not in evidence_pathway_signals(item):
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:pathway_incompatible"
            )
            continue
        if not claim_passage_overlap_ok(claim, [item.passage_text or ""]):
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:insufficient_overlap"
            )
            continue

        # Q-10 / 10C.5: only a clean qualifier PASS may raise corroboration confidence.
        # contradicted / insufficient / uncertain must not grant independent confirmation.
        q_assess = assess_claim_qualifier_entailment(claim, [item])
        if q_assess.decision == QualifierDecision.CONTRADICTED:
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:qualifier_conflict:"
                + ",".join((q_assess.reasons or ["conflict"])[:2])
            )
            continue
        if q_assess.decision == QualifierDecision.INSUFFICIENT:
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:qualifier_insufficient:"
                + ",".join((q_assess.reasons or ["insufficient"])[:2])
            )
            continue
        if q_assess.decision == QualifierDecision.UNCERTAIN:
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:qualifier_uncertain:"
                + ",".join((q_assess.reasons or ["uncertain"])[:2])
            )
            continue
        if q_assess.decision != QualifierDecision.PASS:
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:qualifier_not_pass"
            )
            continue

        item_profile = extract_qualifier_profile(item.passage_text or "")
        # Same amount but different/missing cohort must not corroborate a scoped claim.
        if claim_profile.cohorts:
            shared_cohorts = claim_profile.cohorts & item_profile.cohorts
            if not shared_cohorts:
                rejections.append(
                    f"corroboration_rejected:{item.evidence_id}:cohort_mismatch"
                )
                continue
        # Claim with dates cannot be corroborated by a passage that omits period scope.
        if claim_profile.dates and not item_profile.dates:
            rejections.append(
                f"corroboration_rejected:{item.evidence_id}:missing_effective_period"
            )
            continue
        # Same amount but different legal period.
        if claim_profile.dates and item_profile.dates:
            if claim_profile.dates.isdisjoint(item_profile.dates):
                # Allow if roles differ but overlapping dates exist; otherwise reject.
                claim_date_vals = {d for _, d in claim_profile.dates}
                item_date_vals = {d for _, d in item_profile.dates}
                if claim_date_vals.isdisjoint(item_date_vals):
                    rejections.append(
                        f"corroboration_rejected:{item.evidence_id}:temporal_period_mismatch"
                    )
                    continue

        # Candidate accepted as affirmative independent corroboration.
        return CorroborationSearchResult(item=item, rejection_reasons=rejections)

    if not rejections:
        rejections.append("corroboration_unavailable_no_candidate")
    return CorroborationSearchResult(item=None, rejection_reasons=rejections)


def _claim_polar_conflict(claim: str, passages: list[str]) -> str | None:
    """Detect hard antonym conflicts (e.g. unlimited vs limited hours)."""
    blob = " ".join(passages or [])
    if not claim or not blob:
        return None
    for claim_pat, evidence_pat in _POLAR_CONFLICTS:
        if claim_pat.search(claim) and evidence_pat.search(blob):
            # Claim itself already acknowledges the restrictive wording → not a conflict.
            if evidence_pat.search(claim):
                continue
            return "polar_conflict_with_cited_passage"
    return None


# Re-exported aliases so existing verification helpers keep stable local names.
_COHORT_SCOPE_PATTERNS = COHORT_SCOPE_PATTERNS
_CONDITIONAL_QUALIFIER_RE = CONDITIONAL_QUALIFIER_RE
_UNCONDITIONAL_AMOUNT_EXCLUSIVITY_RE = re.compile(
    r"(?i)\b("
    r"is\s+only|are\s+only|only\s+exactly|is\s+exactly|must\s+always\s+be|"
    r"always\s+(?:is|costs?|requires?)|no\s+other\s+threshold"
    r")\b"
)
_BARE_EXCLUSIVITY_RE = re.compile(r"(?i)\b(must\s+be|is\s+exactly|always)\b")


@dataclass(frozen=True)
class _ScopeProfile:
    cohorts: frozenset[str]
    dates: frozenset[tuple[str, str]]
    is_conditional: bool


def _extract_scope_profile(text: str) -> _ScopeProfile:
    blob = text or ""
    cohorts = frozenset(
        name for name, pat in _COHORT_SCOPE_PATTERNS if pat.search(blob)
    )
    mentions = extract_effective_date_mentions(blob)
    dates = frozenset(
        (m.role, m.parsed_date.isoformat() if m.parsed_date else m.date_literal)
        for m in mentions
    )
    is_cond = bool(cohorts or dates or _CONDITIONAL_QUALIFIER_RE.search(blob))
    return _ScopeProfile(cohorts=cohorts, dates=dates, is_conditional=is_cond)


def _has_unconditional_exclusivity(claim: str) -> bool:
    """True for assertions of a universal sole figure; False for scope-limiting 'apply only to'."""
    text = claim or ""
    if _UNCONDITIONAL_AMOUNT_EXCLUSIVITY_RE.search(text):
        return True
    if _BARE_EXCLUSIVITY_RE.search(text) and not _CONDITIONAL_QUALIFIER_RE.search(text):
        return True
    # Plain "only" counts as amount exclusivity ONLY when not part of a scope-limiting clause.
    if re.search(r"(?i)\bonly\b", text) and not _CONDITIONAL_QUALIFIER_RE.search(text):
        return True
    return False


def _curated_rule_applies_on_date(
    item: EvidenceItem,
    reference_date: date | None,
) -> tuple[bool, str]:
    tprov = item.temporal_provenance or {}
    eff_status = str(tprov.get("effective_date_status") or "unknown").lower()
    if eff_status in {"unknown", "partially_known"}:
        return False, f"curated_rule_{eff_status}"
    if reference_date is None:
        return True, "no_reference_date"
    eff_from = parse_unambiguous_date_literal(tprov.get("effective_from"))
    eff_to = parse_unambiguous_date_literal(tprov.get("effective_to"))
    from_inc = bool(tprov.get("effective_from_inclusive", True))
    to_inc = bool(tprov.get("effective_to_inclusive", True))
    if eff_from is None:
        return False, "curated_rule_missing_effective_from"
    if from_inc and reference_date < eff_from:
        return False, "curated_rule_before_effective_from"
    if not from_inc and reference_date <= eff_from:
        return False, "curated_rule_before_exclusive_effective_from"
    if eff_to is not None:
        if to_inc and reference_date > eff_to:
            return False, "curated_rule_after_effective_to"
        if not to_inc and reference_date >= eff_to:
            return False, "curated_rule_on_or_after_exclusive_effective_to"
        return True, "curated_rule_within_bounds"
    if eff_status in {"known_open_ended", "announced_future"}:
        return True, "curated_rule_open_ended"
    return False, f"curated_rule_incompatible_status:{eff_status}"


def _scoped_passage_text_for_claim(claim: str, cited_text: str) -> str:
    """Keep sentences matching the claim's amounts (or amount-free context)."""
    claim_qs = extract_numeric_quantities(
        claim or "", include_years=False, include_bare_integers=True
    )
    if not claim_qs or not (cited_text or "").strip():
        return cited_text
    sentences = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", cited_text.strip())
        if s and s.strip()
    ]
    if len(sentences) <= 1:
        return cited_text
    kept: list[str] = []
    for sent in sentences:
        sent_qs = extract_numeric_quantities(
            sent, include_years=False, include_bare_integers=True
        )
        if not sent_qs or any(
            quantity_matches_evidence(cq, sq).matched
            for cq in claim_qs
            for sq in sent_qs
        ):
            kept.append(sent)
    return " ".join(kept) if kept else cited_text


def _claim_conflicts_with_evidence(
    claim: str,
    cited: list[EvidenceItem],
    all_evidence: list[EvidenceItem],
    *,
    resolved_reference_date: date | None = None,
) -> str | None:
    """Detect genuine same-scope contradictions while allowing compatible cohort/period rules."""
    claim_qs = extract_numeric_quantities(
        claim or "", include_years=False, include_bare_integers=True
    )
    if not claim_qs:
        return None
    exclusive = _has_unconditional_exclusivity(claim)
    claim_scope = _extract_scope_profile(claim)
    claim_paths = set(detect_pathway_ids(claim))
    claim_mentions = extract_effective_date_mentions(
        claim or "", reference_date=resolved_reference_date
    )

    claim_frames_historical = bool(
        re.search(
            r"(?i)\b(previously|former(ly)?|historical|no longer applies|until|before)\b",
            claim or "",
        )
    )
    claim_asserts_current = bool(
        re.search(
            r"(?i)\b(currently|now|today|at present|current\s+(?:threshold|fee|rule|rate))\b",
            claim or "",
        )
    )
    claim_frames_future = (not claim_asserts_current) and (
        any(m.is_future_for_reference for m in claim_mentions)
        or bool(_FUTURE_MODAL_RE.search(claim or ""))
    )
    cited_blob = " ".join(c.passage_text or "" for c in cited)
    cited_mentions_all = extract_effective_date_mentions(
        _scoped_passage_text_for_claim(claim or "", cited_blob),
        reference_date=resolved_reference_date,
    )
    cited_marks_historical = bool(
        _HISTORICAL_RE.search(_scoped_passage_text_for_claim(claim or "", cited_blob))
        or any(m.is_expired_for_reference for m in cited_mentions_all)
    )
    # 1. Cited passage explicitly rejects the claimed amount.
    for item in cited:
        text = item.passage_text or ""
        item_qs = quantities_from_evidence_item(item)
        item_scope = _extract_scope_profile(text)
        same_or_unscoped_cohort = (
            not claim_scope.cohorts
            or not item_scope.cohorts
            or bool(claim_scope.cohorts & item_scope.cohorts)
        )
        if (
            any(quantity_matches_evidence(cq, iq).matched for cq in claim_qs for iq in item_qs)
            and re.search(
                r"(?i)\b(does not apply|no longer|incorrect|not\s+the\s+threshold)\b",
                text,
            )
            and not claim_frames_historical
            and same_or_unscoped_cohort
        ):
            return "cited_passage_rejects_claimed_amount"
        has_conflict_in_item = bool(
            item_qs
            and all(
                not any(quantity_matches_evidence(cq, iq).matched for iq in item_qs)
                for cq in claim_qs
            )
            and any(quantities_conflict(cq, iq) for cq in claim_qs for iq in item_qs)
        )
        if (
            exclusive
            and has_conflict_in_item
            and same_or_unscoped_cohort
            and not _CONDITIONAL_QUALIFIER_RE.search(claim or "")
            and not (claim_scope.dates and item_scope.dates and claim_scope.dates != item_scope.dates)
        ):
            return "conflicting_official_amount_in_cited_passage"

    # 2. Applicable reviewed LegalRule records override conflicting unverified passages
    #    for the same pathway + cohort + effective date.
    cited_ids = {c.evidence_id for c in cited}
    for item in all_evidence:
        if item.source_type != EvidenceSourceType.CURATED_RULE:
            continue
        if item.evidence_id in cited_ids:
            continue
        rule_ok, _ = _curated_rule_applies_on_date(item, resolved_reference_date)
        if not rule_ok:
            continue
        tprov = item.temporal_provenance or {}
        prov = item.provenance or {}
        rule_pathway = prov.get("pathway_id")
        if claim_paths and rule_pathway and rule_pathway not in claim_paths:
            continue
        rule_cohort = str(tprov.get("cohort_key") or "general").lower()
        rule_is_trans = bool(tprov.get("is_transitional"))
        # Different cohort rules are compatible, not contradictory.
        if rule_is_trans or rule_cohort != "general":
            if not claim_scope.cohorts:
                continue
        elif claim_scope.cohorts:
            # General reviewed rule vs cohort-specific/transitional claim -> compatible.
            continue
        if claim_frames_future:
            continue
        rule_eff_from = parse_unambiguous_date_literal(tprov.get("effective_from"))
        if claim_frames_historical and not claim_asserts_current:
            if resolved_reference_date is None:
                continue
            claim_years = [int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", claim or "")]
            prior_year_only = bool(
                rule_eff_from is not None
                and claim_years
                and all(y < rule_eff_from.year for y in claim_years)
            )
            explicit_expired_in_claim = any(
                m.is_expired_for_reference for m in claim_mentions
            )
            if explicit_expired_in_claim or prior_year_only or (
                cited_marks_historical
                and not (
                    rule_eff_from is not None
                    and claim_years
                    and any(y >= rule_eff_from.year for y in claim_years)
                    and not re.search(r"(?i)\b(until|before)\b", claim or "")
                )
            ):
                continue
        item_qs = quantities_from_evidence_item(item)
        if item_qs and all(
            not any(quantity_matches_evidence(cq, iq).matched for iq in item_qs)
            for cq in claim_qs
        ) and any(quantities_conflict(cq, iq) for cq in claim_qs for iq in item_qs):
            return "conflicts_with_reviewed_legal_rule"

    # 3. Broader corpus: same pathway, same cohort/period scope, unconditional conflict.
    for item in all_evidence:
        text = item.passage_text or ""
        item_qs = quantities_from_evidence_item(item)
        item_paths = set(evidence_pathway_signals(item))
        if claim_paths and item_paths and claim_paths.isdisjoint(item_paths):
            continue
        item_scope = _extract_scope_profile(text)
        same_or_unscoped_cohort = (
            not claim_scope.cohorts
            or not item_scope.cohorts
            or bool(claim_scope.cohorts & item_scope.cohorts)
        )
        if (
            any(quantity_matches_evidence(cq, iq).matched for cq in claim_qs for iq in item_qs)
            and re.search(
                r"(?i)\b(does not apply|no longer|incorrect|not\s+the\s+threshold)\b",
                text,
            )
            and not claim_frames_historical
            and same_or_unscoped_cohort
            and not _CONDITIONAL_QUALIFIER_RE.search(claim or "")
        ):
            return "evidence_rejects_claimed_amount"
        if all(
            any(quantity_matches_evidence(cq, iq).matched for iq in item_qs)
            for cq in claim_qs
        ):
            continue
        if not item_qs or not any(
            quantities_conflict(cq, iq) for cq in claim_qs for iq in item_qs
        ):
            continue
        pathway_aligned = (
            (claim_paths and item_paths and bool(claim_paths & item_paths))
            or (not claim_paths and not item_paths)
        )
        if not (exclusive and pathway_aligned):
            continue
        # Compatible if distinct cohorts or distinct effective-date boundaries exist.
        if (claim_scope.cohorts or item_scope.cohorts) and (
            claim_scope.cohorts != item_scope.cohorts
        ):
            continue
        if claim_scope.is_conditional or item_scope.is_conditional:
            if (claim_scope.dates or item_scope.dates) and (
                claim_scope.dates != item_scope.dates
            ):
                continue
            if claim_scope.cohorts != item_scope.cohorts:
                continue
        return "conflicting_official_amount_on_same_pathway"
    return None


def _claim_has_material_number(claim: str) -> bool:
    """True when a claim asserts a material numeric that should not leak if unsupported."""
    if numeric_literals_in_text(claim):
        return True
    return bool(_MATERIAL_BARE_NUM_RE.search(claim or ""))


def _claim_discloses_cohort(claim: str, item: EvidenceItem) -> bool:
    tprov = item.temporal_provenance or {}
    cohort_key = str(tprov.get("cohort_key") or "general").lower()
    if cohort_key == "general" and not tprov.get("is_transitional"):
        return True
    low = (claim or "").lower()
    if cohort_key in low or "transitional" in low:
        return True
    scope = _extract_scope_profile(claim)
    return bool(scope.cohorts or scope.is_conditional)


def _temporal_status_for_claim(
    claim: str,
    cited: list[EvidenceItem],
    *,
    future_effective_present: bool,
    expired_present: bool = False,
    ambiguous_dates_present: bool = False,
    review_reasons: list[str],
    resolved_reference_date: date | None = None,
    temporal_mode: str | None = None,
    temporal_period_year: int | None = None,
    user_circumstances: dict[str, Any] | None = None,
    clarification_needed: bool = False,
) -> tuple[ClaimVerificationStatus | None, str | None]:
    """Evaluate temporal status with exact reference-date & curated-rule checks."""
    curated = [c for c in cited if c.source_type == EvidenceSourceType.CURATED_RULE]
    non_curated = [c for c in cited if c.source_type != EvidenceSourceType.CURATED_RULE]

    # 1. Check curated reviewed LegalRule items deterministically first.
    curated_covers_numerics = False
    if curated:
        for rule_item in curated:
            ok, reason = _curated_rule_applies_on_date(
                rule_item, resolved_reference_date
            )
            if not ok:
                return ClaimVerificationStatus.TEMPORAL_UNCERTAINTY, reason
            tprov = rule_item.temporal_provenance or {}
            is_cohort_restricted = (
                bool(tprov.get("is_transitional"))
                or str(tprov.get("cohort_key") or "general").lower() != "general"
            )
            if is_cohort_restricted and not _claim_discloses_cohort(claim, rule_item):
                return (
                    ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
                    "cohort_restriction_omitted_from_claim",
                )
            if (
                is_cohort_restricted
                and not user_circumstances
                and not clarification_needed
            ):
                # Retained with qualification if claim explicitly names the cohort.
                pass
        curated_blob = " ".join(r.passage_text or "" for r in curated)
        claim_lits = numeric_literals_in_text(claim)
        curated_covers_numerics = bool(claim_lits) and all(
            passage_contains_literal(curated_blob, lit) for lit in claim_lits
        )
        if not non_curated or curated_covers_numerics:
            for item in non_curated:
                lstat = (item.legal_status or "unknown").strip().lower()
                if lstat in {"withdrawn", "proposed", "proposed_not_effective", "proposed_or_preparatory"}:
                    return (
                        ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
                        f"cited_source_non_enacted_status:{lstat}",
                    )
            if clarification_needed or temporal_period_year is not None:
                return (
                    ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
                    "year_only_or_ambiguous_period_clarification",
                )
            return None, None

    # 2. Check legal_status on non-curated cited passages.
    for item in non_curated:
        lstat = (item.legal_status or "unknown").strip().lower()
        if lstat in {"withdrawn", "proposed", "proposed_not_effective", "proposed_or_preparatory"}:
            return (
                ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
                f"cited_source_non_enacted_status:{lstat}",
            )
        if lstat in {"archived", "historical", "superseded"}:
            if temporal_mode == "historical":
                return (
                    ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
                    f"historical_snapshot_unverified:{lstat}",
                )
            if not re.search(
                r"(?i)\b(previously|former|historical|until|before|archived|superseded)\b",
                claim,
            ):
                return (
                    ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
                    f"non_current_source_presented_without_framing:{lstat}",
                )

    # 3. Evaluate passage date mentions against resolved_reference_date (scoped to cited sentence/claim).
    raw_cited_text = " ".join(c.passage_text or "" for c in non_curated)
    cited_text = _scoped_passage_text_for_claim(claim, raw_cited_text)
    ref_for_mentions = resolved_reference_date or datetime.now(timezone.utc).date()
    cited_mentions = extract_effective_date_mentions(
        cited_text, reference_date=ref_for_mentions
    )
    cited_future = any(m.is_future_for_reference for m in cited_mentions)
    cited_expired = any(m.is_expired_for_reference for m in cited_mentions)
    cited_ambiguous = any(
        m.ambiguous or m.boundary_unresolved for m in cited_mentions
    )

    if cited_ambiguous:
        return (
            ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
            "ambiguous_or_unresolved_effective_date_boundary",
        )

    blob = " ".join([claim, cited_text])
    has_past_start_without_modal = bool(
        _FUTURE_RE.search(blob)
        and not _FUTURE_MODAL_RE.search(blob)
        and any(
            m.role == "start" and m.parsed_date is not None and not m.is_future_for_reference
            for m in cited_mentions
        )
    )
    if cited_future or (_FUTURE_RE.search(blob) and not has_past_start_without_modal):
        # Future rule relative to resolved_reference_date.
        if temporal_mode == "future" and resolved_reference_date is not None:
            future_starts = [
                m.parsed_date
                for m in cited_mentions
                if m.role == "start" and m.parsed_date is not None
            ]
            if future_starts and all(d <= resolved_reference_date for d in future_starts):
                return None, None
        cited_has_future_signal = cited_future or (
            bool(_FUTURE_RE.search(cited_text)) and not has_past_start_without_modal
        )
        if re.search(r"(?i)\b(currently|now|today|at present)\b", claim) or (
            not re.search(r"(?i)\b(from|will|future|effective)\b", claim)
            and numeric_literals_in_text(claim)
            and cited_has_future_signal
        ):
            return (
                ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
                "future_effective_rule_presented_as_current",
            )
        if cited_has_future_signal:
            return (
                ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
                "future_effective_date_present",
            )

    if cited_expired:
        if not re.search(
            r"(?i)\b(previously|former|historical|until|before|ended|expired)\b",
            claim,
        ):
            return (
                ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
                "expired_or_sunset_rule_without_historical_framing",
            )

    if _HISTORICAL_RE.search(blob) and numeric_literals_in_text(claim):
        if not re.search(r"(?i)\b(previously|former|historical|until|before)\b", claim):
            if _HISTORICAL_RE.search(cited_text):
                return (
                    ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
                    "historical_figure_without_historical_framing",
                )

    if temporal_mode == "historical" and any(
        bool((item.temporal_provenance or {}).get("is_current_version", True))
        for item in non_curated
    ):
        return (
            ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
            "historical_candidate_from_current_page_unverified",
        )

    if any("potentially_outdated" in r or "pending_change" in r for r in review_reasons):
        if numeric_literals_in_text(claim) or detect_high_risk_categories(claim):
            return (
                ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
                "pending_change_or_potentially_outdated_queue",
            )

    if clarification_needed or temporal_period_year is not None:
        return (
            ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
            "period_or_date_clarification_required",
        )

    return None, None


def _suppressed_numeric_literals(
    suppressed_claims: list[str], retained: list[AnswerClaim]
) -> list[str]:
    """Numerics that appear only in suppressed claims (not in retained claims)."""
    kept_blob = " ".join(c.claim for c in retained)
    kept_compact = normalize_numeric_literal(kept_blob)
    out: list[str] = []
    seen: set[str] = set()
    for claim in suppressed_claims:
        literals = list(material_literals_for_scrub(claim))
        for m in _MATERIAL_BARE_NUM_RE.finditer(claim or ""):
            literals.append(m.group(0))
        for lit in literals:
            compact = normalize_numeric_literal(lit)
            if not compact or compact in kept_compact:
                continue
            if compact in seen:
                continue
            # Skip lone calendar years that are common prose noise.
            if re.fullmatch(r"(?:19|20)\d{2}", compact):
                continue
            seen.add(compact)
            out.append(lit)
    return out


def _scrub_suppressed_numerics_from_text(text: str, literals: list[str]) -> str:
    """Remove gap/uncertainty sentences that reintroduce suppressed numerics."""
    if not text or not literals:
        return text
    compact_lits = [normalize_numeric_literal(x) for x in literals if x]
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    kept_parts: list[str] = []
    for part in parts:
        compact = normalize_numeric_literal(part)
        if any(lit and lit in compact for lit in compact_lits):
            # Drop only when this looks like a gap / uncertainty echo.
            if re.search(
                r"(?i)\b("
                r"important gaps remain|whether|confirm|unconfirmed|"
                r"not clearly|should be checked|before relying|"
                r"uncertainty|missing information"
                r")\b",
                part,
            ) or re.search(r"(?i)important gaps remain", text):
                # If the whole answer has a gaps section, scrub the literal from the part.
                cleaned = part
                for lit in literals:
                    cleaned = re.sub(re.escape(lit), "[figure omitted]", cleaned, flags=re.I)
                # Prefer dropping gap clauses that exist only to echo the figure.
                if re.search(r"(?i)whether.+threshold|whether.+figure|secret .+deposit", cleaned):
                    continue
                part = cleaned
        kept_parts.append(part)
    rebuilt = " ".join(p for p in kept_parts if p and p.strip())
    # Also scrub remaining literal occurrences in gap blocks.
    for lit in literals:
        if normalize_numeric_literal(lit) and normalize_numeric_literal(lit) in normalize_numeric_literal(
            rebuilt
        ):
            # Only scrub outside retained factual sentences: replace in "Important gaps" segment.
            if "Important gaps remain:" in rebuilt:
                head, sep, tail = rebuilt.partition("Important gaps remain:")
                for item in literals:
                    tail = re.sub(re.escape(item), "an unconfirmed figure", tail, flags=re.I)
                rebuilt = head + sep + tail
    return rebuilt.strip()


def _scrub_missing_information(
    missing: list[str], literals: list[str]
) -> list[str]:
    if not literals:
        return missing
    cleaned: list[str] = []
    for item in missing:
        compact = normalize_numeric_literal(item)
        if any(normalize_numeric_literal(lit) in compact for lit in literals if lit):
            # Replace with a non-numeric gap description.
            cleaned.append(
                "Confirmation of the applicable official numerical requirement "
                "from the correct pathway page"
            )
            continue
        cleaned.append(item)
    # Deduplicate while preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for item in cleaned:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _source_authority_gate(
    claim: str,
    cited: list[EvidenceItem],
    *,
    jurisdiction: str,
) -> tuple[ClaimVerificationStatus | None, str | None]:
    """Block normative verification from statistical datasets, portal stubs, or EU-only sources."""
    if not cited:
        return None, None
    jur = (jurisdiction or "").strip().upper()
    cats = [getattr(c, "source_authority_category", "unknown") for c in cited]
    is_normative_claim = bool(
        numeric_literals_in_text(claim)
        or detect_high_risk_categories(claim)
        or re.search(
            r"(?i)\b(must|required|requires|requirement|minimum|threshold|fee|eligible|eligibility|qualify|permitted)\b",
            claim or "",
        )
    )
    if all(cat == SourceAuthorityCategory.GOVERNMENT_STATISTICAL_DATASET.value for cat in cats):
        if is_normative_claim:
            return (
                ClaimVerificationStatus.INSUFFICIENT_EVIDENCE,
                "statistical_dataset_cannot_verify_normative_requirement",
            )
    if all(cat == SourceAuthorityCategory.GOVERNMENT_PORTAL.value for cat in cats):
        if numeric_literals_in_text(claim) or detect_high_risk_categories(claim):
            return (
                ClaimVerificationStatus.INSUFFICIENT_EVIDENCE,
                "portal_page_cannot_verify_normative_requirement",
            )
    if jur != "EU" and all(
        bool((c.provenance or {}).get("eu_level_source"))
        or "directive_may_need_national_implementation" in (c.legal_status or "")
        for c in cited
    ):
        if is_normative_claim:
            return (
                ClaimVerificationStatus.INSUFFICIENT_EVIDENCE,
                "eu_level_source_cannot_verify_national_implementation",
            )
    # Narrow exclusivity / universal truncation gate remains here; broader
    # truncation + qualifier entailment is handled by assess_claim_qualifier_entailment.
    if any(
        getattr(c, "is_truncated", False) or "…[truncated]" in (c.passage_text or "")
        for c in cited
    ) and (
        _has_unconditional_exclusivity(claim)
        or re.search(
            r"(?i)\b(all\s+applicants|no\s+exceptions|always\s+exempt|without\s+exception)\b",
            claim or "",
        )
    ):
        return (
            ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
            "cited_excerpt_truncated_qualifiers_may_be_omitted",
        )
    return None, None


def verify_material_claims(
    *,
    answer: str | None,
    claims: list[AnswerClaim],
    evidence: list[EvidenceItem],
    jurisdiction: str,
    pathway_id: str | None,
    question: str = "",
    missing_information: list[str] | None = None,
    corroborator: OfficialCorroborator | None = None,
    max_external_corroborations: int = 0,
    resolved_reference_date: date | None = None,
    temporal_mode: str | None = None,
    temporal_outcome: str | None = None,
    temporal_period_year: int | None = None,
    user_circumstances: dict[str, Any] | None = None,
    clarification_needed: bool = False,
) -> VerificationResult:
    """Verify grounded claims and rebuild the user-visible answer.

    Deterministic by default. Optional corroborator may perform at most
    ``max_external_corroborations`` official fetches for high-risk gaps.
    """
    started = time.perf_counter()
    warnings: list[str] = []
    missing = list(missing_information or [])
    by_id = {e.evidence_id: e for e in evidence}
    jur = (jurisdiction or "").strip().upper()
    q_cats = detect_high_risk_categories(question)
    review_reasons = review_queue_uncertainty(
        jurisdiction=jur, pathway_id=pathway_id
    )
    date_notes = reconcile_effective_dates(
        evidence, reference_date=resolved_reference_date
    )
    future_effective = bool(date_notes.future_effective_mentions)
    expired_present = bool(date_notes.expired_mentions)
    ambiguous_dates = bool(
        date_notes.ambiguous_date_mentions or date_notes.unresolved_boundary_mentions
    )
    if date_notes.warnings:
        warnings.extend(date_notes.warnings)
    if review_reasons:
        warnings.extend(review_reasons)
    if temporal_outcome:
        warnings.append(f"temporal_selection:{temporal_outcome}")

    outcomes: list[ClaimVerificationOutcome] = []
    retained: list[AnswerClaim] = []
    suppressed: list[str] = []
    qualified: list[str] = []
    remaining_uncertainties: list[str] = []
    independent_checks = 0
    external_calls = 0
    working_evidence = list(evidence)

    for claim in claims:
        cited: list[EvidenceItem] = []
        invalid = False
        for eid in claim.evidence_ids:
            item = by_id.get(eid)
            if item is None:
                invalid = True
                break
            if item.jurisdiction != jur:
                invalid = True
                break
            cited.append(item)

        if invalid or not cited:
            outcomes.append(
                ClaimVerificationOutcome(
                    claim=claim.claim,
                    evidence_ids=list(claim.evidence_ids),
                    status=ClaimVerificationStatus.INSUFFICIENT_EVIDENCE,
                    reasons=["citation_unresolved_or_cross_jurisdiction"],
                    original_uncertainty=claim.uncertainty,
                    retained=False,
                )
            )
            suppressed.append(claim.claim)
            continue

        passages = [c.passage_text for c in cited]
        pathway_result = classify_pathway_applicability(
            claim=claim.claim,
            cited=cited,
            pathway_id=pathway_id,
        )

        status: ClaimVerificationStatus | None = None
        reasons: list[str] = []
        revised_uncertainty = claim.uncertainty
        corroboration = IndependentCorroboration.NOT_REQUIRED

        if _ELIGIBILITY_OVERCLAIM_RE.search(claim.claim):
            status = ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
            reasons.append("eligibility_overclaim_without_verified_personal_facts")
        else:
            auth_status, auth_reason = _source_authority_gate(
                claim.claim, cited, jurisdiction=jur
            )
            if auth_status is not None:
                status = auth_status
                reasons.append(auth_reason or "source_authority_insufficient")
        if status is None:
            # Phase 10D.4 strict policy: high-risk claims need human-verified rule evidence.
            try:
                from lifepilot.governance.verification_policy import (
                    strict_mode_blocks_high_risk_claim,
                )

                blocked, block_reason = strict_mode_blocks_high_risk_claim(
                    question=question,
                    claim_text=claim.claim,
                    evidence=working_evidence,
                )
                if blocked:
                    status = ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
                    reasons.append(block_reason or "strict_verified_only")
            except Exception:  # noqa: BLE001
                pass
        if status is not None:
            pass
        elif should_drop_for_pathway(pathway_result, claim=claim.claim):
            status = ClaimVerificationStatus.PATHWAY_MISMATCH
            reasons.append(
                f"pathway:{pathway_result.classification.value}:{pathway_result.reason}"
            )
        elif pathway_result.classification == PathwayApplicability.CROSS_PATHWAY:
            status = ClaimVerificationStatus.PATHWAY_MISMATCH
            reasons.append(pathway_result.reason)
        else:
            num_diag = diagnose_claim_numeric_support(claim.claim, cited)
            active_num_contradiction = (
                not num_diag.supported
                and num_diag.mismatch_kind in {"currency_mismatch", "unit_mismatch", "percent_mismatch"}
                and (
                    num_diag.reason.startswith("currency_mismatch:")
                    or num_diag.reason.startswith("unit_period_mismatch:")
                    or num_diag.reason.startswith("duration_unit_mismatch:")
                    or num_diag.reason in {"duration_vs_monetary_unit_mismatch", "percent_unit_mismatch", "unit_category_mismatch"}
                )
            )
            # Temporal gates first for dated numeric claims so future/historical
            # figures are not lost to weak lexical overlap on acronym-heavy excerpts.
            early_temporal: ClaimVerificationStatus | None = None
            early_t_reason: str | None = None
            if numeric_literals_in_text(claim.claim) and num_diag.supported:
                early_temporal, early_t_reason = _temporal_status_for_claim(
                    claim.claim,
                    cited,
                    future_effective_present=future_effective,
                    expired_present=expired_present,
                    ambiguous_dates_present=ambiguous_dates,
                    review_reasons=review_reasons,
                    resolved_reference_date=resolved_reference_date,
                    temporal_mode=temporal_mode,
                    temporal_period_year=temporal_period_year,
                    user_circumstances=user_circumstances,
                    clarification_needed=clarification_needed,
                )
            if early_temporal is not None:
                if early_t_reason and (
                    early_t_reason.startswith("curated_rule_")
                    or "cited_source_non_enacted_status" in early_t_reason
                ):
                    conflict = _claim_conflicts_with_evidence(
                        claim.claim,
                        cited,
                        working_evidence,
                        resolved_reference_date=resolved_reference_date,
                    )
                    if conflict:
                        status = ClaimVerificationStatus.CONTRADICTED
                        reasons.append(conflict)
                    else:
                        status = ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
                        reasons.append(early_t_reason)
                else:
                    status = early_temporal
                    reasons.append(early_t_reason or "temporal_or_pending_change_uncertainty")
            elif active_num_contradiction:
                status = ClaimVerificationStatus.CONTRADICTED
                reasons.append(num_diag.reason)
            elif not claim_passage_overlap_ok(claim.claim, passages):
                status = ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
                reasons.append("insufficient_passage_overlap")
                # Still apply qualifier checks so stripped conditions / inversions
                # are not retained merely because overlap already failed.
                q_assess = assess_claim_qualifier_entailment(claim.claim, cited)
                if q_assess.decision == QualifierDecision.CONTRADICTED:
                    status = ClaimVerificationStatus.CONTRADICTED
                    reasons.extend(q_assess.reasons or ["qualifier_conflict"])
                elif q_assess.decision == QualifierDecision.INSUFFICIENT:
                    reasons.extend(
                        q_assess.reasons or ["qualifier_entailment_insufficient"]
                    )
                elif q_assess.decision == QualifierDecision.UNCERTAIN:
                    reasons.extend(
                        q_assess.reasons or ["qualifier_entailment_uncertain"]
                    )
            elif not num_diag.supported:
                status = ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
                reasons.append(num_diag.reason)
            else:
                polar = _claim_polar_conflict(claim.claim, passages)
                if polar:
                    status = ClaimVerificationStatus.CONTRADICTED
                    reasons.append(polar)
                else:
                    conflict = _claim_conflicts_with_evidence(
                        claim.claim,
                        cited,
                        working_evidence,
                        resolved_reference_date=resolved_reference_date,
                    )
                    if conflict:
                        status = ClaimVerificationStatus.CONTRADICTED
                        reasons.append(conflict)
                    else:
                        # Phase 10C.3: qualifier / negation / conditional entailment.
                        q_assess = assess_claim_qualifier_entailment(
                            claim.claim, cited
                        )
                        if q_assess.decision == QualifierDecision.CONTRADICTED:
                            status = ClaimVerificationStatus.CONTRADICTED
                            reasons.extend(q_assess.reasons or ["qualifier_conflict"])
                        elif q_assess.decision == QualifierDecision.INSUFFICIENT:
                            status = ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
                            reasons.extend(
                                q_assess.reasons or ["qualifier_entailment_insufficient"]
                            )
                        elif q_assess.decision == QualifierDecision.UNCERTAIN:
                            status = ClaimVerificationStatus.TEMPORAL_UNCERTAINTY
                            reasons.extend(
                                q_assess.reasons or ["qualifier_entailment_uncertain"]
                            )

        if status is None:
            temporal, t_reason = _temporal_status_for_claim(
                claim.claim,
                cited,
                future_effective_present=future_effective,
                expired_present=expired_present,
                ambiguous_dates_present=ambiguous_dates,
                review_reasons=review_reasons,
                resolved_reference_date=resolved_reference_date,
                temporal_mode=temporal_mode,
                temporal_period_year=temporal_period_year,
                user_circumstances=user_circumstances,
                clarification_needed=clarification_needed,
            )
            if temporal is not None:
                if t_reason and (
                    t_reason.startswith("curated_rule_")
                    or "cited_source_non_enacted_status" in t_reason
                ):
                    status = ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
                    reasons.append(t_reason)
                else:
                    status = temporal
                    reasons.append(t_reason or "temporal_or_pending_change_uncertainty")

        cites_verified_rule = any(
            c.source_type == EvidenceSourceType.CURATED_RULE
            and c.verification_state == EvidenceVerificationState.HUMAN_VERIFIED_RULE
            for c in cited
        )
        high_risk = _is_high_risk_claim(claim.claim, q_cats)
        corr_item: EvidenceItem | None = None
        corr_rejections: list[str] = []
        if (
            status is None
            and high_risk
            and numeric_literals_in_text(claim.claim)
            and not cites_verified_rule
        ):
            independent_checks += 1
            corr_search = search_distinct_corroboration(
                claim=claim.claim,
                cited=cited,
                all_evidence=working_evidence,
                pathway_id=pathway_id,
            )
            corr_rejections = list(corr_search.rejection_reasons)
            corr_item = corr_search.item
            if corr_item is not None:
                corroboration = IndependentCorroboration.REUSED_RETRIEVAL_DISTINCT_SOURCE
                reasons.append(f"distinct_source_corroboration:{corr_item.evidence_id}")
            elif (
                corroborator is not None
                and external_calls < max(0, max_external_corroborations)
            ):
                external_calls += 1
                try:
                    fetched = corroborator.corroborate(
                        claim=claim.claim,
                        jurisdiction=jur,
                        pathway_id=pathway_id,
                        existing_evidence=working_evidence,
                    )
                    if fetched:
                        working_evidence.extend(fetched)
                        for e in fetched:
                            by_id[e.evidence_id] = e
                        corr_search2 = search_distinct_corroboration(
                            claim=claim.claim,
                            cited=cited,
                            all_evidence=working_evidence,
                            pathway_id=pathway_id,
                        )
                        corr_rejections.extend(corr_search2.rejection_reasons)
                        corr_item = corr_search2.item
                        if corr_item is not None:
                            corroboration = IndependentCorroboration.SEPARATE_OFFICIAL
                            reasons.append(
                                f"separate_official_corroboration:{corr_item.evidence_id}"
                            )
                        else:
                            corroboration = IndependentCorroboration.UNAVAILABLE
                            reasons.append("separate_official_fetch_unconfirmed")
                            if corr_rejections:
                                reasons.append(
                                    "corroboration_incompatible:"
                                    + corr_rejections[-1].split(":", 2)[-1][:80]
                                )
                            status = ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
                    else:
                        corroboration = IndependentCorroboration.UNAVAILABLE
                        reasons.append("separate_official_fetch_empty")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "verification.corroboration_failed: %s", exc.__class__.__name__
                    )
                    corroboration = IndependentCorroboration.UNAVAILABLE
                    reasons.append(f"corroboration_error:{exc.__class__.__name__}")
                    warnings.append(
                        "independent_official_corroboration_failed:"
                        f"{exc.__class__.__name__}"
                    )
                    if not revised_uncertainty:
                        revised_uncertainty = (
                            "Independent official corroboration failed; "
                            "treat the figure as unconfirmed."
                        )
            else:
                corroboration = IndependentCorroboration.UNAVAILABLE
                # Still allow support from deterministic checks, but qualify.
                if not revised_uncertainty:
                    revised_uncertainty = (
                        "Figure not independently corroborated from a second "
                        "distinct official passage in this request."
                    )
                reasons.append("no_distinct_official_corroboration")
                if corr_rejections:
                    reasons.append(
                        "corroboration_candidates_rejected:"
                        + str(min(len(corr_rejections), 9))
                    )

        if status is None:
            if pathway_result.classification == PathwayApplicability.RELATED_CONTEXT:
                # Other-pathway numeric context is not a requirement for the requested
                # pathway unless the claim is an explicit comparison.
                if numeric_literals_in_text(
                    claim.claim
                ) and not has_explicit_comparison_language(claim.claim):
                    status = ClaimVerificationStatus.PATHWAY_MISMATCH
                    reasons.append("related_context_numeric_not_explicit_comparison")
                else:
                    status = ClaimVerificationStatus.SUPPORTED
                    reasons.append("related_context_qualified")
                    if not revised_uncertainty:
                        revised_uncertainty = (
                            "Cited figure/rule may relate to a different pathway; "
                            "not treated as a confirmed requirement for the requested pathway."
                        )
            elif (
                pathway_result.classification == PathwayApplicability.INSUFFICIENT
                and pathway_id
                and numeric_literals_in_text(claim.claim)
                and pathway_result.reason != "no_pathway_markers_in_evidence"
            ):
                # Incomplete pathway markers for comparison/locality — abstain.
                # Unmarked excerpts fall through to supported after grounding checks.
                status = ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
                reasons.append(pathway_result.reason or "pathway_markers_insufficient")
            else:
                status = ClaimVerificationStatus.SUPPORTED
                reasons.append(
                    pathway_result.reason or "deterministic_grounding_support"
                )

        retained_flag = status in {
            ClaimVerificationStatus.SUPPORTED,
            ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
            ClaimVerificationStatus.INSUFFICIENT_EVIDENCE,
        }
        # Drop contradicted / pathway-mismatched material claims from the answer.
        if status in {
            ClaimVerificationStatus.CONTRADICTED,
            ClaimVerificationStatus.PATHWAY_MISMATCH,
        }:
            retained_flag = False
            suppressed.append(claim.claim)
        elif status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE:
            # Drop unsupported exact figures, eligibility overclaims, qualifier failures,
            # and normative claims relying solely on statistical datasets, portal stubs,
            # or unimplemented EU sources.
            qualifier_drop = any(
                r.startswith(_QUALIFIER_DROP_REASON_PREFIXES) for r in reasons
            )
            drop_insufficient = (
                _claim_has_material_number(claim.claim)
                or "eligibility_overclaim_without_verified_personal_facts" in reasons
                or "statistical_dataset_cannot_verify_normative_requirement" in reasons
                or "portal_page_cannot_verify_normative_requirement" in reasons
                or "eu_level_source_cannot_verify_national_implementation" in reasons
                or qualifier_drop
                or (
                    "insufficient_passage_overlap" in reasons
                    and claim_has_high_risk_legal_assertion(claim.claim)
                )
            )
            if drop_insufficient:
                retained_flag = False
                suppressed.append(claim.claim)
                if _claim_has_material_number(claim.claim):
                    gap = "confirmation of unsupported numerical requirement"
                    if gap not in missing:
                        missing.append(gap)
                elif "statistical_dataset_cannot_verify_normative_requirement" in reasons:
                    gap = "official normative pathway guidance (statistical dataset cannot verify requirements)"
                    if gap not in missing:
                        missing.append(gap)
                elif "eu_level_source_cannot_verify_national_implementation" in reasons:
                    gap = "national implementing guidance (EU-level instrument alone does not establish national rules)"
                    if gap not in missing:
                        missing.append(gap)
                elif "eligibility_overclaim_without_verified_personal_facts" in reasons:
                    gap = (
                        "personal facts and full eligibility conditions required "
                        "before any eligibility conclusion"
                    )
                    if gap not in missing:
                        missing.append(gap)
                elif qualifier_drop:
                    gap = (
                        "full qualifying conditions, exceptions, and scope from the "
                        "cited official passages"
                    )
                    if gap not in missing:
                        missing.append(gap)
            else:
                retained_flag = True
                if not revised_uncertainty:
                    revised_uncertainty = "Evidence is incomplete for this point."
                qualified.append(claim.claim)
        elif status == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY:
            retained_flag = True
            ref_label = (
                resolved_reference_date.isoformat()
                if resolved_reference_date is not None
                else "the requested date"
            )
            if any("future_effective" in r for r in reasons):
                temporal_note = (
                    f"Future-effective requirement — NOT confirmed as currently applicable on {ref_label}; "
                    "do not rely on as current law."
                )
            elif any("expired_or_sunset" in r or "historical_figure" in r for r in reasons):
                temporal_note = (
                    f"Expired or historical requirement — NOT confirmed as currently applicable on {ref_label}; "
                    "do not treat as confirmed current law."
                )
            elif any("historical" in r for r in reasons):
                temporal_note = (
                    f"Historical candidate evidence — applicability on {ref_label} is not human-verified."
                )
            elif any("cohort_restriction_omitted" in r for r in reasons):
                temporal_note = (
                    "Applies only to a restricted applicant cohort; do not treat as a general rule."
                )
            elif any(
                "cited_excerpt_truncated" in r or "truncated_evidence_may_omit" in r
                for r in reasons
            ):
                temporal_note = (
                    "Cited excerpt is truncated — qualifying conditions or exceptions may lie outside "
                    "this excerpt; do not treat as an exhaustive rule."
                )
            elif any("unrecognized_evidence_conditions" in r for r in reasons):
                temporal_note = (
                    "Cited passages refer to further conditions that were not fully extracted; "
                    "do not treat the claim as an exhaustive eligibility rule."
                )
            elif temporal_period_year is not None and any(
                "year_only" in r or "period_or_date_clarification" in r for r in reasons
            ):
                temporal_note = (
                    f"Year-only period query ({temporal_period_year}) without an exact as_of_date — "
                    f"applicability on a specific date within {temporal_period_year} is unconfirmed; "
                    "do not treat as confirmed current law."
                )
            else:
                temporal_note = (
                    "Effective date / current applicability is unresolved; "
                    "do not treat as confirmed current law."
                )
            if revised_uncertainty:
                if temporal_note.lower() not in revised_uncertainty.lower():
                    revised_uncertainty = f"{temporal_note} {revised_uncertainty.strip()}"
            else:
                revised_uncertainty = temporal_note
            qualified.append(claim.claim)
            remaining_uncertainties.append(revised_uncertainty)
        elif revised_uncertainty:
            qualified.append(claim.claim)
            remaining_uncertainties.append(revised_uncertainty)

        evidence_refs = build_claim_evidence_refs(
            claim.claim,
            cited,
            corroborating=corr_item if retained_flag else None,
            include_excerpt=True,
        )
        provenance_incomplete = any(
            (not r.excerpt_authenticated and r.excerpt)
            or "external_evidence_document_id_unavailable" in r.provenance_limitations
            or not r.evidence_id
            for r in evidence_refs
        )
        if not cited:
            provenance_incomplete = True

        outcome = ClaimVerificationOutcome(
            claim=claim.claim,
            evidence_ids=[c.evidence_id for c in cited],
            status=status,
            reasons=reasons,
            original_uncertainty=claim.uncertainty,
            revised_uncertainty=revised_uncertainty,
            retained=retained_flag,
            independent_corroboration=corroboration,
            corroborating_evidence_id=(
                corr_item.evidence_id if corr_item is not None and retained_flag else None
            ),
            evidence_refs=evidence_refs if retained_flag else [],
            provenance_incomplete=bool(provenance_incomplete and retained_flag),
            corroboration_rejection_reasons=corr_rejections[:12],
        )
        outcomes.append(outcome)
        if retained_flag:
            retained.append(
                AnswerClaim(
                    claim=claim.claim,
                    evidence_ids=[c.evidence_id for c in cited],
                    uncertainty=revised_uncertainty,
                )
            )

    elapsed = (time.perf_counter() - started) * 1000
    suppressed_nums = _suppressed_numeric_literals(suppressed, retained)
    if suppressed_nums:
        missing = _scrub_missing_information(missing, suppressed_nums)

    new_answer, _ = compose_grounded_answer_text(
        answer or "",
        retained,
        missing_information=missing,
        suppressed_claims=suppressed,
    )
    if not retained:
        status_hint = "insufficient_evidence"
        new_answer = (
            "Available evidence was insufficient to support specific immigration "
            "claims for this question."
        )
        if missing:
            new_answer += " Important gaps remain: " + "; ".join(missing[:5]) + "."
        new_answer += (
            " Based only on the retrieved passages for this request; "
            "not complete legal advice or an eligibility determination."
        )
    else:
        status_hint = None

    if suppressed_nums:
        new_answer = _scrub_suppressed_numerics_from_text(new_answer, suppressed_nums)
        warnings.append(
            "verification_scrubbed_suppressed_numerics_from_prose:"
            + str(len(suppressed_nums))
        )

    if suppressed:
        warnings.append(f"verification_suppressed_claims:{len(suppressed)}")
    if qualified:
        warnings.append(f"verification_qualified_claims:{len(qualified)}")
    warnings.append(
        "Automated verification assesses evidence support only; "
        "it is not human legal certification."
    )

    report = VerificationReport(
        claim_outcomes=outcomes,
        suppressed_claims=suppressed,
        qualified_claims=qualified,
        remaining_uncertainties=list(dict.fromkeys(remaining_uncertainties)),
        warnings=list(dict.fromkeys(warnings)),
        verification_ms=round(elapsed, 3),
        independent_checks=independent_checks,
        external_calls=external_calls,
        estimated_verification_cost_usd=0.0,
        high_risk_categories=q_cats,
    )
    return VerificationResult(
        answer=new_answer.strip(),
        claims=retained,
        outcomes=outcomes,
        warnings=report.warnings,
        missing_information=missing,
        status_hint=status_hint,
        report=report,
        pre_verification_claims=list(claims),
    )

