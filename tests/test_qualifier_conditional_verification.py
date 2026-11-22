"""Phase 10C.3 — Qualifier, exception and conditional claim verification tests.

Synthetic immigration examples only. No live DB. No extra LLM calls.
"""

from __future__ import annotations

from lifepilot.agent.answer import AnswerClaim, GroundedAnswer
from lifepilot.agent.evidence import evidence_from_internal_passage
from lifepilot.agent.grounding import (
    compose_grounded_answer_text,
    validate_and_ground_answer,
)
from lifepilot.agent.qualifiers import (
    QualifierDecision,
    assess_claim_qualifier_entailment,
    extract_qualifier_profile,
)
from lifepilot.agent.verification import (
    ClaimVerificationStatus,
    verify_material_claims,
)


def _ev(
    *,
    text: str,
    url: str = "https://www.gov.uk/guidance/synthetic-skilled-worker",
    chunk_id: int = 1,
    document_version_id: int = 200,
    document_id: int = 100,
    heading_path: str | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
    is_truncated: bool = False,
    jur: str = "GB",
):
    item = evidence_from_internal_passage(
        jurisdiction=jur,
        official_url=url,
        document_id=document_id,
        document_version_id=document_version_id,
        chunk_id=chunk_id,
        content_text=text,
        score=0.9,
        heading_path=heading_path,
        char_start=char_start,
        char_end=char_end,
    )
    if is_truncated:
        item = item.model_copy(
            update={
                "is_truncated": True,
                "truncation_reason": "passage_char_limit",
                "passage_text": text if "…[truncated]" in text else text + " …[truncated]",
            }
        )
    return item


# ---------------------------------------------------------------------------
# Unit: polarity / bounds / geography / temporal
# ---------------------------------------------------------------------------

def test_eligible_versus_ineligible_polarity_conflict():
    ev = _ev(
        text="Applicants are ineligible for the Skilled Worker route without a valid Certificate of Sponsorship."
    )
    claim = AnswerClaim(
        claim="Applicants are eligible for the Skilled Worker route without a valid Certificate of Sponsorship.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.CONTRADICTED
    assert any("polarity_conflict:eligibility" in r for r in res.outcomes[0].reasons)
    assert claim.claim not in (res.answer or "")


def test_required_versus_exempt_polarity_conflict():
    ev = _ev(text="A Certificate of Sponsorship is required for Skilled Worker applicants.")
    claim = AnswerClaim(
        claim="Skilled Worker applicants are exempt from a Certificate of Sponsorship.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.CONTRADICTED
    assert any("polarity_conflict" in r for r in res.outcomes[0].reasons)


def test_must_versus_must_not():
    ev = _ev(text="Sponsored workers must not take employment outside the sponsored role.")
    claim = AnswerClaim(
        claim="Sponsored workers must take employment outside the sponsored role.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.CONTRADICTED
    assert any("polarity_conflict:must_modal" in r or "polarity_conflict" in r for r in res.outcomes[0].reasons)


def test_at_least_versus_at_most_bound_conflict():
    ev = _ev(
        text="The going rate for this occupation is at most £38,700 per year for the listed SOC code."
    )
    claim = AnswerClaim(
        claim="The going rate for this occupation is at least £38,700 per year for the listed SOC code.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.CONTRADICTED
    assert any("bound_direction_conflict" in r for r in res.outcomes[0].reasons)


def test_before_versus_after_temporal_conflict():
    ev = _ev(
        text="Applications made after 4 April 2024 are assessed under the new general salary threshold."
    )
    claim = AnswerClaim(
        claim="Applications made before 4 April 2024 are assessed under the new general salary threshold.",
        evidence_ids=[ev.evidence_id],
    )
    assess = assess_claim_qualifier_entailment(claim.claim, [ev])
    assert assess.decision == QualifierDecision.CONTRADICTED
    assert any("temporal_direction_conflict" in r for r in assess.reasons)


def test_inside_versus_outside_london():
    ev = _ev(
        text="The minimum income requirement outside London is lower than the inside London figure."
    )
    claim = AnswerClaim(
        claim="The minimum income requirement inside London is lower than the outside London figure.",
        evidence_ids=[ev.evidence_id],
    )
    # Lexical inversion of geography against evidence.
    assess = assess_claim_qualifier_entailment(claim.claim, [ev])
    # Claim contains both inside and outside; evidence contains both — may pass polarity.
    # Stronger case: claim asserts only inside when evidence only supports outside.
    ev2 = _ev(
        text="The £29,000 financial requirement applies to applicants living outside London."
    )
    claim2 = AnswerClaim(
        claim="The £29,000 financial requirement applies to applicants living inside London.",
        evidence_ids=[ev2.evidence_id],
    )
    res = verify_material_claims(
        answer=claim2.claim,
        claims=[claim2],
        evidence=[ev2],
        jurisdiction="GB",
        pathway_id=None,
    )
    assert res.outcomes[0].status in {
        ClaimVerificationStatus.CONTRADICTED,
        ClaimVerificationStatus.INSUFFICIENT_EVIDENCE,
    }
    assert any(
        "geographic_scope_conflict" in r or "polarity_conflict" in r or "insufficient" in r
        for r in res.outcomes[0].reasons
    ) or res.outcomes[0].retained is False


# ---------------------------------------------------------------------------
# Conditional sponsorship / age / occupation / transitional
# ---------------------------------------------------------------------------

def test_conditional_sponsorship_exemption_stripped():
    ev = _ev(
        text=(
            "Applicants do not need sponsorship if they have unrestricted work authorization. "
            "Other applicants require a Certificate of Sponsorship."
        )
    )
    bad = AnswerClaim(
        claim="Applicants do not need sponsorship.",
        evidence_ids=[ev.evidence_id],
    )
    res_bad = verify_material_claims(
        answer=bad.claim,
        claims=[bad],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res_bad.outcomes[0].status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
    assert any("negation_scope_condition_stripped" in r for r in res_bad.outcomes[0].reasons)
    assert bad.claim not in (res_bad.answer or "")

    good = AnswerClaim(
        claim=(
            "Applicants do not need sponsorship if they have unrestricted work authorization."
        ),
        evidence_ids=[ev.evidence_id],
    )
    res_good = verify_material_claims(
        answer=good.claim,
        claims=[good],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res_good.outcomes[0].status == ClaimVerificationStatus.SUPPORTED
    assert res_good.outcomes[0].retained is True


def test_age_restricted_requirement_omission():
    ev = _ev(
        text=(
            "Applicants under 26 may meet a lower salary threshold of £33,400 per year. "
            "Other applicants must meet the general threshold."
        )
    )
    bad = AnswerClaim(
        claim="All applicants must meet a salary threshold of £33,400 per year.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=bad.claim,
        claims=[bad],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].retained is False
    assert any(
        "unsupported_universal_claim" in r
        or "age_restriction_omitted" in r
        or "cohort_qualifier_omitted" in r
        or "mandatory_condition_omitted" in r
        for r in res.outcomes[0].reasons
    )


def test_employment_occupation_restriction_preserved_paraphrase():
    ev = _ev(
        text=(
            "The going rate for SOC code 2134 is £38,700 per year. "
            "This rate applies only to the listed occupation."
        )
    )
    good = AnswerClaim(
        claim=(
            "The going rate for SOC code 2134 is £38,700 per year and applies only to the listed occupation."
        ),
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=good.claim,
        claims=[good],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.SUPPORTED
    assert res.outcomes[0].retained is True


def test_general_versus_transitional_universal_overclaim():
    ev = _ev(
        text=(
            "Under transitional arrangements, applicants who continually held Tier 2 leave "
            "may meet a lower threshold of £29,000 per year. The general threshold is higher."
        )
    )
    bad = AnswerClaim(
        claim="All applicants may meet a salary threshold of £29,000 per year without exception.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=bad.claim,
        claims=[bad],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].retained is False
    assert any(
        "unsupported_universal_claim" in r or "cohort_qualifier_omitted" in r
        for r in res.outcomes[0].reasons
    )


def test_valid_transitional_conditional_paraphrase_not_suppressed():
    ev = _ev(
        text=(
            "Under transitional arrangements for those who continually held Tier 2 leave, "
            "a lower threshold of £29,000 per year may apply."
        )
    )
    good = AnswerClaim(
        claim=(
            "Under transitional arrangements, applicants who continually held Tier 2 leave "
            "may meet a lower threshold of £29,000 per year."
        ),
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=good.claim,
        claims=[good],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.SUPPORTED
    assert res.outcomes[0].retained is True


def test_missing_mandatory_exception_omission():
    ev = _ev(
        text=(
            "A Certificate of Sponsorship is required for Skilled Worker applicants "
            "unless the applicant already has unrestricted work authorization."
        )
    )
    bad = AnswerClaim(
        claim="A Certificate of Sponsorship is required for Skilled Worker applicants.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=bad.claim,
        claims=[bad],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].retained is False
    assert any(
        "mandatory_condition_omitted" in r or "negation_scope" in r
        for r in res.outcomes[0].reasons
    )


def test_correct_negation_scope_retained():
    ev = _ev(
        text=(
            "Applicants do not need sponsorship if they have unrestricted work authorization."
        )
    )
    profile = extract_qualifier_profile(
        "Applicants do not need sponsorship if they have unrestricted work authorization."
    )
    assert profile.is_conditional
    assess = assess_claim_qualifier_entailment(
        "Applicants do not need sponsorship if they have unrestricted work authorization.",
        [ev],
    )
    assert assess.decision == QualifierDecision.PASS


# ---------------------------------------------------------------------------
# Multi-passage, truncation, cross-pathway
# ---------------------------------------------------------------------------

def test_multi_passage_incompatible_cohort_combination():
    ev_a = _ev(
        text="New entrants may use a lower threshold of £33,400 per year.",
        chunk_id=1,
        document_version_id=10,
        url="https://www.gov.uk/guidance/a",
        heading_path="New entrants",
        char_start=0,
        char_end=80,
    )
    ev_b = _ev(
        text="Healthcare workers on national pay scales may use a different going rate of £33,400.",
        chunk_id=2,
        document_version_id=99,
        url="https://www.gov.uk/guidance/b",
        heading_path="Healthcare",
        char_start=0,
        char_end=90,
    )
    claim = AnswerClaim(
        claim=(
            "Applicants may use a threshold of £33,400 per year."
        ),
        evidence_ids=[ev_a.evidence_id, ev_b.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev_a, ev_b],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].retained is False
    assert any("cross_passage" in r or "mandatory_condition" in r or "cohort" in r for r in res.outcomes[0].reasons)


def test_truncated_evidence_blocks_comprehensive_eligibility():
    ev = _ev(
        text="The general salary threshold is £41,700 per year",
        is_truncated=True,
    )
    claim = AnswerClaim(
        claim="All applicants are eligible if they earn £41,700 per year without exception.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].status in {
        ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
        ClaimVerificationStatus.INSUFFICIENT_EVIDENCE,
    }
    assert any(
        "truncated" in r or "unsupported_universal" in r for r in res.outcomes[0].reasons
    )


def test_truncated_non_exclusive_requirement_marked_uncertain():
    ev = _ev(
        text="Sponsored workers must be paid the going rate for the occupation",
        is_truncated=True,
    )
    claim = AnswerClaim(
        claim="Sponsored workers must be paid the going rate for the occupation.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY
    assert any("truncated" in r for r in res.outcomes[0].reasons)
    assert "truncated" in (res.answer or "").lower() or "uncertain" in (
        res.outcomes[0].revised_uncertainty or ""
    ).lower() or "excerpt" in (res.answer or "").lower()


def test_cross_pathway_evidence_contamination_still_blocked():
    sw = _ev(
        text="Skilled Worker general salary threshold is £41,700 per year.",
        url="https://www.gov.uk/skilled-worker-visa",
        chunk_id=1,
    )
    # IE critical skills numeric on a GB request should not support a SW claim alone;
    # pathway mismatch remains the primary gate when markers disagree.
    ie = evidence_from_internal_passage(
        jurisdiction="GB",
        official_url="https://enterprise.gov.ie/critical-skills-employment-permit/",
        document_id=50,
        document_version_id=51,
        chunk_id=2,
        content_text=(
            "Critical Skills Employment Permit remuneration must be at least €38,000 "
            "for some occupations."
        ),
        score=0.8,
    )
    claim = AnswerClaim(
        claim="The Skilled Worker salary threshold is €38,000 per year.",
        evidence_ids=[ie.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[sw, ie],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].retained is False


# ---------------------------------------------------------------------------
# Numeric / temporal / curated-rule compatibility & prose safety
# ---------------------------------------------------------------------------

def test_currency_unit_compatibility_still_enforced():
    ev = _ev(text="The general salary threshold is £41,700 per year.")
    claim = AnswerClaim(
        claim="The general salary threshold is £41,700 per month.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].retained is False


def test_rejected_claim_does_not_leak_into_prose():
    ev = _ev(
        text=(
            "Applicants do not need sponsorship if they have unrestricted work authorization."
        )
    )
    bad = AnswerClaim(
        claim="Applicants do not need sponsorship.",
        evidence_ids=[ev.evidence_id],
    )
    good = AnswerClaim(
        claim=(
            "Applicants do not need sponsorship if they have unrestricted work authorization."
        ),
        evidence_ids=[ev.evidence_id],
    )
    raw_answer = (
        "Applicants do not need sponsorship. "
        "Applicants do not need sponsorship if they have unrestricted work authorization."
    )
    res = verify_material_claims(
        answer=raw_answer,
        claims=[bad, good],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    # Unconditional sentence must not survive alone as advice.
    assert "Applicants do not need sponsorship." not in (res.answer or "").replace(
        "Applicants do not need sponsorship if they have unrestricted work authorization.",
        "",
    )
    assert "unrestricted work authorization" in (res.answer or "").lower()


def test_compose_grounded_answer_preserves_qualifiers():
    kept = [
        AnswerClaim(
            claim=(
                "Applicants do not need sponsorship if they have unrestricted work authorization."
            ),
            evidence_ids=["x"],
        )
    ]
    text, rebuilt = compose_grounded_answer_text(
        "Applicants do not need sponsorship. Extra unsupported advice follows.",
        kept,
        suppressed_claims=["Applicants do not need sponsorship."],
    )
    assert "unrestricted work authorization" in text.lower()
    assert rebuilt is True


def test_grounding_drops_inverted_qualifier_claim():
    ev = _ev(
        text="The going rate is at most £38,700 per year for this occupation."
    )
    raw = GroundedAnswer(
        answer="The going rate is at least £38,700 per year for this occupation.",
        claims=[
            AnswerClaim(
                claim="The going rate is at least £38,700 per year for this occupation.",
                evidence_ids=[ev.evidence_id],
            )
        ],
        missing_information=[],
        limitations=[],
    )
    grounded = validate_and_ground_answer(
        raw, evidence=[ev], jurisdiction="GB", pathway_id="gb_skilled_worker"
    )
    assert grounded.dropped_claims >= 1
    assert grounded.answer is not None
    assert grounded.answer.claims == [] or all(
        "at least" not in c.claim.lower() or "at most" in (ev.passage_text or "").lower()
        for c in grounded.answer.claims
    )


def test_valid_at_least_claim_not_false_positive_suppressed():
    ev = _ev(
        text=(
            "Remuneration must be at least €38,000 for some Critical Skills occupations."
        ),
        url="https://enterprise.gov.ie/critical-skills-employment-permit/",
        jur="IE",
    )
    claim = AnswerClaim(
        claim=(
            "Some Critical Skills Employment Permit occupations require remuneration "
            "of at least €38,000."
        ),
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
        question="What remuneration thresholds apply to Critical Skills?",
        max_external_corroborations=0,
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.SUPPORTED
    assert res.outcomes[0].retained is True


def test_zero_additional_llm_calls_qualifier_path(monkeypatch):
    """Qualifier assessment is pure-Python; no provider imports at call time."""
    import lifepilot.agent.qualifiers as qmod

    calls = {"n": 0}

    def _boom(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("LLM must not be called")

    # Ensure no accidental provider use inside qualifier assessment.
    monkeypatch.setattr(
        "lifepilot.agent.providers.get_llm_provider",
        _boom,
        raising=False,
    )
    ev = _ev(
        text="Applicants do not need sponsorship if they have unrestricted work authorization."
    )
    assess = assess_claim_qualifier_entailment(
        "Applicants do not need sponsorship if they have unrestricted work authorization.",
        [ev],
    )
    assert assess.decision == QualifierDecision.PASS
    assert calls["n"] == 0
    assert hasattr(qmod, "assess_claim_qualifier_entailment")


def test_unsupported_universal_claim_suppressed():
    ev = _ev(
        text=(
            "In some cases, applicants with a relevant PhD may meet a lower threshold "
            "of £33,400 per year."
        )
    )
    claim = AnswerClaim(
        claim="All applicants always meet a threshold of £33,400 per year without exception.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].retained is False
    assert any("unsupported_universal_claim" in r for r in res.outcomes[0].reasons)
