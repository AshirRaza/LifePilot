"""Deterministic citation validation and conservative claim grounding."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lifepilot.agent.answer import AnswerClaim, GroundedAnswer
from lifepilot.agent.evidence import EvidenceItem, EvidenceSourceType
from lifepilot.agent.numeric import (
    canonical_quantity_signature,
    diagnose_claim_numeric_support,
    extract_numeric_quantities,
)
from lifepilot.agent.pathway_applicability import (
    classify_pathway_applicability,
    normalize_numeric_literal,
    should_drop_for_pathway,
)
from lifepilot.agent.qualifiers import (
    QualifierDecision,
    assess_claim_qualifier_entailment,
    claim_has_high_risk_legal_assertion,
    prose_preserves_material_qualifiers,
)

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}", re.IGNORECASE)
# Conservative overlap: claim tokens should appear in cited passages.
_MIN_OVERLAP_RATIO = 0.28
_MIN_OVERLAP_TOKENS = 4
# Sentence retention vs grounded claims when reshaping free-text answers.
_SENTENCE_CLAIM_OVERLAP_RATIO = 0.30
_SENTENCE_CLAIM_MIN_TOKENS = 3

_ELIGIBILITY_CONCLUSION_RE = re.compile(
    r"\b("
    r"you are (fully )?eligible|user is (fully )?eligible|"
    r"fully eligible for every|ineligible for every|"
    r"you are not eligible|guaranteed (approval|eligibility)"
    r")\b",
    re.IGNORECASE,
)
_AFFIRMATIVE_ELIGIBILITY_RE = re.compile(
    r"\b("
    r"you are (fully )?eligible|you qualify|guaranteed (approval|eligibility)|"
    r"definitely eligible|automatically approved"
    r")\b",
    re.IGNORECASE,
)
_NEGATION_WINDOW_RE = re.compile(
    r"(?i)(\bnot\b|\bn't\b|\bcannot\b|\bcan't\b|\bno\b|\bnever\b|\bwithout\b|"
    r"\bdon't\b|\bdo not\b|\bnothing\b|\bbefore\b|\bsay that\b|\bsay you\b|"
    r"\bdeclare that\b|\bdoes not\b|\bi can't\b|\bcannot say\b|"
    r"\bwithout deciding\b|\bwithout concluding\b|\brather than\b)"
)
# Monetary / numeric literals that must appear in cited passages when used in claims.
_NUMERIC_LITERAL_RE = re.compile(
    r"(?:£|\$|€)\s?\d[\d,]*(?:\.\d+)?|"
    r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|"
    r"\b\d+(?:\.\d+)?\s?%|"
    r"\b(?:19|20)\d{2}\b"
)
_IMPL_DISCLAIMER_RE = re.compile(
    r"(?i)("
    r"these passages are unverified retrievals?[^.]*\.|"
    r"unverified retrievals?[^.]*\.|"
    r"retrieved material is not validated pathway evidence[^.]*\.|"
    r"must not be treated as COMPLETE or human-verified[^.]*\.|"
    r"citations confirm evidence was supplied in-request[^.]*\.|"
    r"mechanical citation and/or conservative overlap checks[^.]*\.|"
    r"grounding_dropped_claims:\d+[^.]*\."
    r")"
)
_META_SENTENCE_RE = re.compile(
    r"(?i)\b("
    r"insufficient|missing|uncertain|cannot confirm|can't confirm|"
    r"cannot say|can't say|do not support|does not support|"
    r"not enough evidence|check the official|please verify|"
    r"important gaps|based only on the retrieved|"
    r"not (complete )?legal advice|not a determination of eligibility"
    r")\b"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_USER_FACING_QUALIFIER = (
    "Based only on the retrieved passages for this request; "
    "not complete legal advice or an eligibility determination."
)


@dataclass
class GroundingResult:
    answer: GroundedAnswer | None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dropped_claims: int = 0
    status_hint: str | None = None  # e.g. insufficient_evidence
    answer_rebuilt_from_claims: bool = False


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _overlap_ok(claim: str, passages: list[str]) -> bool:
    claim_tokens = _tokens(claim)
    if not claim_tokens:
        return False
    joined = " ".join(passages)
    passage_tokens = _tokens(joined)
    if not passage_tokens:
        return False
    overlap = claim_tokens & passage_tokens
    ratio = len(overlap) / max(1, len(claim_tokens))
    return len(overlap) >= _MIN_OVERLAP_TOKENS and ratio >= _MIN_OVERLAP_RATIO


def claim_passage_overlap_ok(claim: str, passages: list[str]) -> bool:
    """Public overlap check used by grounding and unified verification."""
    return _overlap_ok(claim, passages)


def claim_numeric_literals_supported(
    claim: str,
    passages: list[str] | list[EvidenceItem],
) -> bool:
    """Reject claims that invent or alter values, currencies, units, or ambiguous figures."""
    return diagnose_claim_numeric_support(
        claim or "", passages or (), include_years=False
    ).supported


def _split_sentences(text: str) -> list[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    parts = _SENTENCE_SPLIT_RE.split(cleaned)
    return [p.strip() for p in parts if p and p.strip()]


def _strip_impl_disclaimers(text: str) -> str:
    cleaned = _IMPL_DISCLAIMER_RE.sub(" ", text or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _affirmative_eligibility(text: str) -> bool:
    for match in _AFFIRMATIVE_ELIGIBILITY_RE.finditer(text or ""):
        window = (text or "")[max(0, match.start() - 80) : match.start()]
        if _NEGATION_WINDOW_RE.search(window):
            continue
        return True
    return False


def _compose_from_claims(claims: list[AnswerClaim]) -> str:
    parts: list[str] = []
    for claim in claims:
        sentence = claim.claim.strip()
        if not sentence:
            continue
        if sentence[-1] not in ".!?":
            sentence += "."
        if claim.uncertainty:
            unc = claim.uncertainty.strip().rstrip(".")
            sentence = f"{sentence} Uncertainty: {unc}."
        parts.append(sentence)
    return " ".join(parts)


_TEMPORAL_OR_COHORT_QUALIFIER_RE = re.compile(
    r"(?i)\b("
    r"transitional|cohort|before\s+\d|until\s+\d|after\s+\d|from\s+\d|"
    r"effective\s+(?:from|on)|unless|only\s+(?:if|for|when|to|before|until)|"
    r"under\s+\d+|over\s+\d+|phd|postdoc|new\s+entrant|"
    r"inside\s+london|outside\s+london|continually\s+held|previously|formerly|"
    r"except|subject\s+to|provided\s+that|at\s+least|at\s+most|must\s+not|"
    r"exempt|ineligible|prohibited|do\s+not\s+need"
    r")\b"
)


def _prose_preserves_temporal_cohort_qualifiers(
    retained: list[str],
    kept: list[AnswerClaim],
) -> bool:
    """True only if retained raw prose retains temporal/cohort/conditional cues."""
    if any(c.uncertainty for c in kept):
        return False
    prose_lower = " ".join(retained).lower()
    for c in kept:
        for m in _TEMPORAL_OR_COHORT_QUALIFIER_RE.finditer(c.claim):
            cue = m.group(0).lower().strip()
            if cue not in prose_lower:
                return False
    if not prose_preserves_material_qualifiers(
        retained, [c.claim for c in kept]
    ):
        return False
    return True


def _should_force_claim_rebuild(kept: list[AnswerClaim]) -> bool:
    """High-risk legal assertions prefer deterministic claim rendering."""
    high_risk = [c for c in kept if claim_has_high_risk_legal_assertion(c.claim)]
    if len(high_risk) >= 2:
        return True
    for c in high_risk:
        if re.search(
            r"(?i)\b(unless|except|only\s+if|subject\s+to|provided\s+that|if\s+they)\b",
            c.claim,
        ):
            return True
    return False


def compose_grounded_answer_text(
    raw_answer: str,
    kept: list[AnswerClaim],
    *,
    missing_information: list[str] | None = None,
    suppressed_claims: list[str] | None = None,
) -> tuple[str, bool]:
    """Build user-visible answer from validated claims; drop unsupported free text.

    Always rebuilds deterministically from validated claims when any claim was
    suppressed, when any retained claim has uncertainty, or when raw prose drops
    temporal/cohort qualifiers from kept claims.
    Returns (answer_text, rebuilt_from_claims_only).
    """
    if not kept:
        return (
            "Available evidence was insufficient to support specific immigration "
            "claims for this question.",
            True,
        )

    suppressed_sigs: set[tuple[str, str, str]] = set()
    suppressed_raws: set[str] = set()
    for sc in suppressed_claims or ():
        for q in extract_numeric_quantities(sc or "", include_years=True, include_bare_integers=True):
            sig = canonical_quantity_signature(q)
            if sig is not None:
                suppressed_sigs.add(sig)
            norm = normalize_numeric_literal(q.raw_literal)
            if norm:
                suppressed_raws.add(norm.lower())
    kept_sigs: set[tuple[str, str, str]] = set()
    kept_raws: set[str] = set()
    for c in kept:
        for q in extract_numeric_quantities(c.claim, include_years=True, include_bare_integers=True):
            sig = canonical_quantity_signature(q)
            if sig is not None:
                kept_sigs.add(sig)
            norm = normalize_numeric_literal(q.raw_literal)
            if norm:
                kept_raws.add(norm.lower())
    forbidden_sigs = suppressed_sigs - kept_sigs
    forbidden_raws = {r for r in suppressed_raws if r and r not in kept_raws}

    claim_blob = " ".join(c.claim for c in kept)
    claim_tokens = _tokens(claim_blob)
    cleaned_raw = _strip_impl_disclaimers(raw_answer)
    retained: list[str] = []

    for sentence in _split_sentences(cleaned_raw):
        if _affirmative_eligibility(sentence):
            continue
        sent_qs = extract_numeric_quantities(
            sentence, include_years=True, include_bare_integers=True
        )
        if any(
            (canonical_quantity_signature(q) in forbidden_sigs)
            or (normalize_numeric_literal(q.raw_literal).lower() in forbidden_raws)
            for q in sent_qs
        ):
            continue
        if _META_SENTENCE_RE.search(sentence):
            # Keep cautious meta language, but still drop orphan numbers not in claims.
            sent_non_year_qs = extract_numeric_quantities(
                sentence, include_years=False, include_bare_integers=True
            )
            if sent_non_year_qs and not claim_numeric_literals_supported(sentence, [claim_blob]):
                continue
            retained.append(sentence)
            continue
        st = _tokens(sentence)
        if not st:
            continue
        overlap = st & claim_tokens
        ratio = len(overlap) / max(1, len(st))
        if len(overlap) < _SENTENCE_CLAIM_MIN_TOKENS or ratio < _SENTENCE_CLAIM_OVERLAP_RATIO:
            continue
        if not claim_numeric_literals_supported(sentence, [claim_blob]):
            continue
        retained.append(sentence)

    # Drop retained sentences that invent combined eligibility from multiple claims
    # without belonging to any single claim's qualifier scope.
    if len(kept) >= 2:
        filtered: list[str] = []
        for sentence in retained:
            if _affirmative_eligibility(sentence):
                continue
            # A sentence that asserts eligibility/requirement must overlap one claim well,
            # not merely the union blob.
            st = _tokens(sentence)
            if not st:
                continue
            best = 0.0
            for c in kept:
                ct = _tokens(c.claim)
                ov = st & ct
                best = max(best, len(ov) / max(1, len(st)))
            if (
                claim_has_high_risk_legal_assertion(sentence)
                and best < _SENTENCE_CLAIM_OVERLAP_RATIO
            ):
                continue
            filtered.append(sentence)
        retained = filtered

    force_rebuild = (
        bool(suppressed_claims)
        or not _prose_preserves_temporal_cohort_qualifiers(retained, kept)
        or _should_force_claim_rebuild(kept)
    )
    rebuilt_from_claims = False
    if not force_rebuild and len(retained) >= 1 and sum(len(s) for s in retained) >= 60:
        text = " ".join(retained)
    else:
        text = _compose_from_claims(kept)
        rebuilt_from_claims = True

    missing = [m.strip() for m in (missing_information or []) if m and str(m).strip()]
    if missing:
        gap = "; ".join(missing[:5])
        if "important gaps remain" not in text.lower():
            text = f"{text} Important gaps remain: {gap}."

    if _USER_FACING_QUALIFIER.lower() not in text.lower():
        text = f"{text} {_USER_FACING_QUALIFIER}"

    return text.strip(), rebuilt_from_claims


def validate_and_ground_answer(
    raw: GroundedAnswer,
    *,
    evidence: list[EvidenceItem],
    jurisdiction: str,
    pathway_id: str | None = None,
) -> GroundingResult:
    """Mechanically validate citations, then apply conservative overlap grounding.

    A valid evidence_id is necessary but not sufficient for semantic support.
    Claims that fail overlap / numeric / pathway-applicability checks are dropped.
    The final user-visible answer is rebuilt so rejected claims cannot survive
    as unsupported free-text.
    """
    warnings: list[str] = []
    errors: list[str] = []
    by_id = {e.evidence_id: e for e in evidence}
    jur = (jurisdiction or "").strip().upper()

    kept: list[AnswerClaim] = []
    suppressed_texts: list[str] = []
    dropped = 0

    for claim in raw.claims:
        if _ELIGIBILITY_CONCLUSION_RE.search(claim.claim):
            warnings.append("dropped_claim:eligibility_conclusion_without_verified_facts")
            suppressed_texts.append(claim.claim)
            dropped += 1
            continue
        cited: list[EvidenceItem] = []
        invalid = False
        for eid in claim.evidence_ids:
            item = by_id.get(eid)
            if item is None:
                warnings.append(f"dropped_claim:unknown_evidence_id:{eid}")
                invalid = True
                break
            if item.jurisdiction != jur:
                warnings.append(
                    f"dropped_claim:cross_jurisdiction:{eid}:{item.jurisdiction}"
                )
                invalid = True
                break
            if (
                item.source_type == EvidenceSourceType.INTERNAL_RAG
                and item.document_version_id is None
            ):
                warnings.append(f"dropped_claim:missing_version:{eid}")
                invalid = True
                break
            cited.append(item)
        if invalid or not cited:
            suppressed_texts.append(claim.claim)
            dropped += 1
            continue

        passages = [c.passage_text for c in cited]
        if not claim_passage_overlap_ok(claim.claim, passages):
            warnings.append(
                "dropped_claim:insufficient_passage_overlap:"
                + ",".join(c.evidence_id for c in cited)
            )
            suppressed_texts.append(claim.claim)
            dropped += 1
            continue
        num_diag = diagnose_claim_numeric_support(claim.claim, cited)
        if not num_diag.supported:
            eids_csv = ",".join(c.evidence_id for c in cited)
            if num_diag.reason == "numeric_literal_not_in_passage":
                warnings.append(f"dropped_claim:numeric_literal_not_in_passage:{eids_csv}")
            else:
                warnings.append(
                    f"dropped_claim:numeric_literal_not_in_passage:{num_diag.reason}:{eids_csv}"
                )
            suppressed_texts.append(claim.claim)
            dropped += 1
            continue

        # Defense-in-depth: qualifier / negation / conditional checks before keep.
        q_assess = assess_claim_qualifier_entailment(claim.claim, cited)
        if q_assess.decision == QualifierDecision.CONTRADICTED:
            warnings.append(
                "dropped_claim:qualifier_conflict:"
                + ",".join((q_assess.reasons or ["qualifier_conflict"])[:2])
            )
            suppressed_texts.append(claim.claim)
            dropped += 1
            continue
        if q_assess.decision == QualifierDecision.INSUFFICIENT:
            warnings.append(
                "dropped_claim:qualifier_insufficient:"
                + ",".join((q_assess.reasons or ["qualifier_insufficient"])[:2])
            )
            suppressed_texts.append(claim.claim)
            dropped += 1
            continue

        pathway_result = classify_pathway_applicability(
            claim=claim.claim,
            cited=cited,
            pathway_id=pathway_id,
        )
        if should_drop_for_pathway(pathway_result, claim=claim.claim):
            warnings.append(
                "dropped_claim:pathway_applicability:"
                f"{pathway_result.classification.value}:{pathway_result.reason}:"
                + ",".join(c.evidence_id for c in cited)
            )
            suppressed_texts.append(claim.claim)
            dropped += 1
            continue

        uncertainty = claim.uncertainty
        if q_assess.decision == QualifierDecision.UNCERTAIN and not uncertainty:
            uncertainty = (
                "Cited excerpt or conditions may be incomplete; "
                "do not treat as an exhaustive eligibility rule."
            )
            warnings.append(
                "qualified_claim:qualifier_uncertain:"
                + ",".join((q_assess.reasons or ["qualifier_uncertain"])[:2])
            )
        if (
            pathway_result.classification.value == "related_context"
            and pathway_id
            and not uncertainty
        ):
            uncertainty = (
                "Cited figure/rule may relate to a different pathway; "
                "not treated as a confirmed requirement for the requested pathway."
            )

        kept.append(
            AnswerClaim(
                claim=claim.claim,
                evidence_ids=[c.evidence_id for c in cited],
                uncertainty=uncertainty,
            )
        )

    # Keep mechanical / provenance caveats in warnings — not duplicated into answer prose.
    warnings.append(
        "Citations confirm evidence was supplied in-request; they do not prove "
        "complete legal sufficiency or human verification."
    )
    if dropped:
        warnings.append(
            f"grounding_dropped_claims:{dropped} "
            "(mechanical citation and/or conservative overlap checks)"
        )

    if not kept:
        missing = list(raw.missing_information) or ["supported_claims"]
        abstain, _ = compose_grounded_answer_text(
            raw.answer or "",
            [],
            missing_information=missing,
        )
        # Prefer a clear abstention without stitching unsupported free-text.
        abstain = (
            "Available evidence was insufficient to support specific immigration "
            "claims for this question."
        )
        if missing:
            abstain += " Important gaps remain: " + "; ".join(missing[:5]) + "."
        abstain += f" {_USER_FACING_QUALIFIER}"
        return GroundingResult(
            answer=GroundedAnswer(
                answer=abstain.strip(),
                claims=[],
                missing_information=missing,
                limitations=[
                    lim
                    for lim in raw.limitations
                    if lim and "Citations confirm evidence" not in lim
                ]
                + ["No claims passed citation grounding checks."],
                follow_up_questions=list(raw.follow_up_questions)[:3],
            ),
            warnings=warnings,
            errors=errors,
            dropped_claims=dropped,
            status_hint="insufficient_evidence",
            answer_rebuilt_from_claims=True,
        )

    answer_text, rebuilt = compose_grounded_answer_text(
        raw.answer or "",
        kept,
        missing_information=list(raw.missing_information),
        suppressed_claims=suppressed_texts,
    )
    if rebuilt:
        warnings.append("answer_rebuilt_from_grounded_claims")

    # User-facing limitations: keep model notes that are not implementation jargon.
    limitations = [
        lim
        for lim in raw.limitations
        if lim
        and "Citations confirm evidence" not in lim
        and "unverified retrieval" not in lim.lower()
        and "COMPLETE" not in lim
    ]

    return GroundingResult(
        answer=GroundedAnswer(
            answer=answer_text,
            claims=kept,
            missing_information=list(raw.missing_information),
            limitations=limitations,
            follow_up_questions=list(raw.follow_up_questions)[:5],
        ),
        warnings=warnings,
        errors=errors,
        dropped_claims=dropped,
        status_hint=None,
        answer_rebuilt_from_claims=rebuilt,
    )
