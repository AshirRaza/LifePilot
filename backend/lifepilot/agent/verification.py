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
from enum import Enum
from typing import Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from lifepilot.agent.answer import AnswerClaim
from lifepilot.agent.evidence import EvidenceItem
from lifepilot.agent.grounding import (
    claim_numeric_literals_supported,
    claim_passage_overlap_ok,
    compose_grounded_answer_text,
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
from lifepilot.agent.retrieval_policy import (
    detect_high_risk_categories,
    reconcile_effective_dates,
    review_queue_uncertainty,
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


def find_distinct_corroboration(
    *,
    claim: str,
    cited: list[EvidenceItem],
    all_evidence: list[EvidenceItem],
    pathway_id: str | None,
) -> EvidenceItem | None:
    """Pick a different-URL passage that supports the same numeric + pathway locality."""
    literals = numeric_literals_in_text(claim)
    if not literals:
        return None
    cited_urls = {_url_key(c.source_url) for c in cited}
    requested = (pathway_id or "").strip() or None
    for item in all_evidence:
        key = _url_key(item.source_url)
        if not key or key in cited_urls:
            continue
        if item.evidence_id in {c.evidence_id for c in cited}:
            continue
        if not all(passage_contains_literal(item.passage_text or "", lit) for lit in literals):
            continue
        if requested and requested not in evidence_pathway_signals(item):
            continue
        if not claim_passage_overlap_ok(claim, [item.passage_text or ""]):
            continue
        return item
    return None


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


def _claim_conflicts_with_evidence(
    claim: str,
    cited: list[EvidenceItem],
    all_evidence: list[EvidenceItem],
) -> str | None:
    """Detect when claim amount conflicts with other official amounts on same pathway theme."""
    claim_amounts = {
        re.sub(r"[\s,]", "", m) for m in _CONFLICT_AMOUNT_RE.findall(claim or "")
    }
    if not claim_amounts:
        return None
    exclusive = bool(re.search(r"(?i)\b(only|must be|is exactly|always)\b", claim))
    claim_paths = set(detect_pathway_ids(claim))

    claim_frames_historical = bool(
        re.search(
            r"(?i)\b(previously|former(ly)?|historical|no longer applies|until|before)\b",
            claim or "",
        )
    )
    # Cited passage says the claimed amount does not apply / is incorrect.
    for item in cited:
        text = item.passage_text or ""
        if (
            any(passage_contains_literal(text, a) for a in claim_amounts)
            and re.search(
                r"(?i)\b(does not apply|no longer|incorrect|not\s+the\s+threshold)\b",
                text,
            )
            and not claim_frames_historical
        ):
            return "cited_passage_rejects_claimed_amount"
        other = {
            re.sub(r"[\s,]", "", m) for m in _CONFLICT_AMOUNT_RE.findall(text)
        }
        if exclusive and other and not (claim_amounts & other):
            return "conflicting_official_amount_in_cited_passage"

    # Broader corpus: same pathway markers, different exclusive figure.
    for item in all_evidence:
        text = item.passage_text or ""
        item_paths = set(evidence_pathway_signals(item))
        if claim_paths and item_paths and claim_paths.isdisjoint(item_paths):
            continue
        if any(passage_contains_literal(text, a) for a in claim_amounts):
            continue
        other = {
            re.sub(r"[\s,]", "", m) for m in _CONFLICT_AMOUNT_RE.findall(text)
        }
        if not other or (claim_amounts & other):
            continue
        pathway_aligned = (
            (claim_paths and item_paths and bool(claim_paths & item_paths))
            or (not claim_paths and not item_paths)
        )
        if exclusive and pathway_aligned:
            return "conflicting_official_amount_on_same_pathway"
    return None


def _claim_has_material_number(claim: str) -> bool:
    """True when a claim asserts a material numeric that should not leak if unsupported."""
    if numeric_literals_in_text(claim):
        return True
    return bool(_MATERIAL_BARE_NUM_RE.search(claim or ""))


def _temporal_status_for_claim(
    claim: str,
    cited: list[EvidenceItem],
    *,
    future_effective_present: bool,
    review_reasons: list[str],
) -> ClaimVerificationStatus | None:
    blob = " ".join([claim] + [c.passage_text or "" for c in cited])
    if future_effective_present or _FUTURE_RE.search(blob):
        # Claiming current applicability without qualification.
        if re.search(r"(?i)\b(currently|now|today|at present)\b", claim) or (
            not re.search(r"(?i)\b(from|will|future|effective)\b", claim)
            and numeric_literals_in_text(claim)
            and _FUTURE_RE.search(" ".join(c.passage_text or "" for c in cited))
        ):
            return ClaimVerificationStatus.TEMPORAL_UNCERTAINTY
        if _FUTURE_RE.search(" ".join(c.passage_text or "" for c in cited)):
            return ClaimVerificationStatus.TEMPORAL_UNCERTAINTY
    if _HISTORICAL_RE.search(blob) and numeric_literals_in_text(claim):
        if not re.search(r"(?i)\b(previously|former|historical|until|before)\b", claim):
            # Historical figure presented without historical framing.
            if _HISTORICAL_RE.search(" ".join(c.passage_text or "" for c in cited)):
                return ClaimVerificationStatus.TEMPORAL_UNCERTAINTY
    if any("potentially_outdated" in r or "pending_change" in r for r in review_reasons):
        if numeric_literals_in_text(claim) or detect_high_risk_categories(claim):
            return ClaimVerificationStatus.TEMPORAL_UNCERTAINTY
    return None


def _suppressed_numeric_literals(
    suppressed_claims: list[str], retained: list[AnswerClaim]
) -> list[str]:
    """Numerics that appear only in suppressed claims (not in retained claims)."""
    kept_blob = " ".join(c.claim for c in retained)
    kept_compact = normalize_numeric_literal(kept_blob)
    out: list[str] = []
    seen: set[str] = set()
    for claim in suppressed_claims:
        literals = list(numeric_literals_in_text(claim))
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
    date_notes = reconcile_effective_dates(evidence)
    future_effective = bool(date_notes.future_effective_mentions)
    if date_notes.warnings:
        warnings.extend(date_notes.warnings)
    if review_reasons:
        warnings.extend(review_reasons)

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
        elif should_drop_for_pathway(pathway_result, claim=claim.claim):
            status = ClaimVerificationStatus.PATHWAY_MISMATCH
            reasons.append(
                f"pathway:{pathway_result.classification.value}:{pathway_result.reason}"
            )
        elif pathway_result.classification == PathwayApplicability.CROSS_PATHWAY:
            status = ClaimVerificationStatus.PATHWAY_MISMATCH
            reasons.append(pathway_result.reason)
        else:
            # Temporal gates first for dated numeric claims so future/historical
            # figures are not lost to weak lexical overlap on acronym-heavy excerpts.
            early_temporal = None
            if numeric_literals_in_text(claim.claim) and claim_numeric_literals_supported(
                claim.claim, passages
            ):
                early_temporal = _temporal_status_for_claim(
                    claim.claim,
                    cited,
                    future_effective_present=future_effective,
                    review_reasons=review_reasons,
                )
            if early_temporal is not None:
                status = early_temporal
                reasons.append("temporal_or_pending_change_uncertainty")
            elif not claim_passage_overlap_ok(claim.claim, passages):
                status = ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
                reasons.append("insufficient_passage_overlap")
            elif not claim_numeric_literals_supported(claim.claim, passages):
                status = ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
                reasons.append("numeric_literal_not_in_passage")
            else:
                polar = _claim_polar_conflict(claim.claim, passages)
                if polar:
                    status = ClaimVerificationStatus.CONTRADICTED
                    reasons.append(polar)
                else:
                    conflict = _claim_conflicts_with_evidence(
                        claim.claim, cited, working_evidence
                    )
                    if conflict:
                        status = ClaimVerificationStatus.CONTRADICTED
                        reasons.append(conflict)

        if status is None:
            temporal = _temporal_status_for_claim(
                claim.claim,
                cited,
                future_effective_present=future_effective,
                review_reasons=review_reasons,
            )
            if temporal is not None:
                status = temporal
                reasons.append("temporal_or_pending_change_uncertainty")

        high_risk = _is_high_risk_claim(claim.claim, q_cats)
        if status is None and high_risk and numeric_literals_in_text(claim.claim):
            independent_checks += 1
            corr = find_distinct_corroboration(
                claim=claim.claim,
                cited=cited,
                all_evidence=working_evidence,
                pathway_id=pathway_id,
            )
            if corr is not None:
                corroboration = IndependentCorroboration.REUSED_RETRIEVAL_DISTINCT_SOURCE
                reasons.append(f"distinct_source_corroboration:{corr.evidence_id}")
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
                        corr2 = find_distinct_corroboration(
                            claim=claim.claim,
                            cited=cited,
                            all_evidence=working_evidence,
                            pathway_id=pathway_id,
                        )
                        if corr2 is not None:
                            corroboration = IndependentCorroboration.SEPARATE_OFFICIAL
                            reasons.append(
                                f"separate_official_corroboration:{corr2.evidence_id}"
                            )
                        else:
                            corroboration = IndependentCorroboration.UNAVAILABLE
                            reasons.append("separate_official_fetch_unconfirmed")
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
            # Drop unsupported exact figures and eligibility overclaims; keep soft
            # qualitative uncertainty only when the claim has no material number.
            drop_insufficient = (
                _claim_has_material_number(claim.claim)
                or "eligibility_overclaim_without_verified_personal_facts" in reasons
            )
            if drop_insufficient:
                retained_flag = False
                suppressed.append(claim.claim)
                if _claim_has_material_number(claim.claim):
                    gap = "confirmation of unsupported numerical requirement"
                    if gap not in missing:
                        missing.append(gap)
                elif "eligibility_overclaim_without_verified_personal_facts" in reasons:
                    gap = (
                        "personal facts and full eligibility conditions required "
                        "before any eligibility conclusion"
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
            if not revised_uncertainty:
                revised_uncertainty = (
                    "Effective date / current applicability is unresolved; "
                    "do not treat as confirmed current law."
                )
            qualified.append(claim.claim)
            remaining_uncertainties.append(revised_uncertainty)
        elif revised_uncertainty:
            qualified.append(claim.claim)
            remaining_uncertainties.append(revised_uncertainty)

        outcome = ClaimVerificationOutcome(
            claim=claim.claim,
            evidence_ids=[c.evidence_id for c in cited],
            status=status,
            reasons=reasons,
            original_uncertainty=claim.uncertainty,
            revised_uncertainty=revised_uncertainty,
            retained=retained_flag,
            independent_corroboration=corroboration,
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

