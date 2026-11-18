"""Phase 10C.1 Numeric, Currency, and Unit Integrity regression suite.

All new thresholds and amounts in this test file are strictly synthetic.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lifepilot.agent.answer import AnswerClaim, GroundedAnswer
from lifepilot.agent.config import AgentConfig, reset_agent_config_cache
from lifepilot.agent.contracts import AgentRequest, AgentStatus
from lifepilot.agent.evidence import (
    EvidenceSourceType,
    evidence_from_external_item,
    evidence_from_internal_passage,
    evidence_from_legal_rule,
)
from lifepilot.agent.grounding import (
    claim_numeric_literals_supported,
    validate_and_ground_answer,
)
from lifepilot.agent.numeric import (
    NumericParseStatus,
    NumericPeriodUnit,
    diagnose_claim_numeric_support,
    extract_numeric_quantities,
    parse_numeric_token,
    parse_single_quantity,
)
from lifepilot.agent.orchestration import (
    OrchestrationResult,
    _numeric_conflict_warnings,
    set_evidence_orchestrator_override,
)
from lifepilot.agent.pathway_applicability import (
    normalize_numeric_literal,
    passage_contains_literal,
)
from lifepilot.agent.providers import (
    LLMUsage,
    StructuredLLMResult,
    set_llm_provider_override,
)
from lifepilot.agent.runner import run_agent
from lifepilot.agent.verification import (
    ClaimVerificationStatus,
    verify_material_claims,
)
from lifepilot.api.rate_limit import reset_rate_limit_state
from lifepilot.api.settings import reset_api_settings_cache


def _ev(
    text: str,
    *,
    jur: str = "GB",
    url: str = "https://example.test/synthetic-route",
    chunk_id: int = 1,
    legal_status: str = "in_force",
):
    return evidence_from_internal_passage(
        jurisdiction=jur,
        official_url=url,
        document_id=800 + chunk_id,
        document_version_id=1800 + chunk_id,
        chunk_id=chunk_id,
        content_text=text,
        score=0.95,
        legal_status=legal_status,
    )


class _SingleCallFakeLLM:
    provider_id = "fake"
    model_id = "fake-10c1"

    def __init__(self, builder):
        self._builder = builder
        self.calls = 0

    def complete(self, *a, **k):
        raise AssertionError("complete() must not be called")

    def complete_structured(self, messages, *, schema, max_tokens, temperature=0.0):
        self.calls += 1
        val = self._builder(messages[-1].content)
        return StructuredLLMResult(
            value=val,
            usage=LLMUsage(input_tokens=40, output_tokens=30, total_tokens=70),
            latency_ms=1.0,
            model=self.model_id,
            provider=self.provider_id,
        )


# ---------------------------------------------------------------------------
# Tests 1–5: Locale-aware Decimal parsing (EU, UK/US, mixed, ambiguous)
# ---------------------------------------------------------------------------


def test_1_european_decimal_commas_parsed_exactly():
    q = parse_single_quantity("€ 423,00")
    assert q is not None
    assert q.status == NumericParseStatus.PARSED
    assert q.value == Decimal("423.00")
    assert q.currency_code == "EUR"

    # Must NEVER turn €423,00 into €42300
    assert normalize_numeric_literal("€ 423,00") == "€423.00"
    assert passage_contains_literal("Synthetic fee is € 423,00.", "€423.00") is True
    assert passage_contains_literal("Synthetic fee is € 423,00.", "€42,300") is False

    q_pct = parse_single_quantity("38,5%")
    assert q_pct is not None
    assert q_pct.value == Decimal("38.5")
    assert q_pct.unit == NumericPeriodUnit.PERCENT


def test_2_uk_us_thousands_and_decimals():
    q1 = parse_single_quantity("£38,700")
    assert q1 is not None and q1.value == Decimal("38700") and q1.currency_code == "GBP"

    q2 = parse_single_quantity("38,700.50")
    assert q2 is not None and q2.value == Decimal("38700.50")

    q3 = parse_single_quantity("€4,238.00")
    assert q3 is not None and q3.value == Decimal("4238.00") and q3.currency_code == "EUR"


def test_3_european_thousands_and_space_thousands():
    q1 = parse_single_quantity("€4.238,00")
    assert q1 is not None and q1.value == Decimal("4238.00") and q1.currency_code == "EUR"

    q2 = parse_single_quantity("38.700,50")
    assert q2 is not None and q2.value == Decimal("38700.50")

    q3 = parse_single_quantity("38 700")
    assert q3 is not None and q3.value == Decimal("38700")

    q4 = parse_single_quantity("1.234.567")
    assert q4 is not None and q4.value == Decimal("1234567")


def test_4_mixed_and_malformed_separators():
    assert passage_contains_literal("Synthetic fee is €4.238,00.", "€4,238.00") is True
    assert passage_contains_literal("Synthetic threshold is 38.700,50.", "38,700.50") is True

    st, val, conv, _ = parse_numeric_token("1,23.45,6")
    assert st == NumericParseStatus.INVALID and val is None and conv == "invalid"

    st2, val2, _, _ = parse_numeric_token("12,,345")
    assert st2 == NumericParseStatus.INVALID and val2 is None


def test_5_ambiguous_dot_three_digits_never_guessed():
    q = parse_single_quantity("38.000")
    assert q is not None
    assert q.status == NumericParseStatus.AMBIGUOUS
    assert q.value is None
    assert q.ambiguity_reason == "ambiguous_dot_three_digit_group"

    # Ambiguous "38.000" must not verify either 38 or 38,000
    assert passage_contains_literal("Threshold is 38.000.", "38,000") is False
    assert passage_contains_literal("Threshold is 38.000.", "38") is False
    assert passage_contains_literal("Threshold is 38,000.", "38.000") is False
    assert passage_contains_literal("Threshold is 38.000.", "38.000") is False

    # Explicit locale_hint resolves intentionally when context is known
    assert parse_numeric_token("38.000", locale_hint="eu")[1] == Decimal("38000")
    assert parse_numeric_token("38.000", locale_hint="gb")[1] == Decimal("38.000")

    # End-to-end verification rejects ambiguous threshold safely
    ev = _ev("Synthetic route salary threshold is €38.000 per year.", jur="NL")
    claim = AnswerClaim(
        claim="The synthetic route salary threshold is €38,000 per year.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev],
        jurisdiction="NL",
        pathway_id=None,
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
    assert res.outcomes[0].retained is False
    assert "38,000" not in (res.answer or "")


# ---------------------------------------------------------------------------
# Tests 6–7: Currency identity & missing/ambiguous currencies
# ---------------------------------------------------------------------------


def test_6_currency_mismatch_and_ambiguous_dollar_rejected():
    ev_eur = _ev("Synthetic Skilled Worker route threshold is €38,700 per year.", jur="IE")
    claim_gbp = AnswerClaim(
        claim="Synthetic Skilled Worker route threshold is £38,700 per year.",
        evidence_ids=[ev_eur.evidence_id],
    )
    res = verify_material_claims(
        answer=claim_gbp.claim,
        claims=[claim_gbp],
        evidence=[ev_eur],
        jurisdiction="IE",
        pathway_id=None,
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.CONTRADICTED
    assert any("currency_mismatch" in r for r in res.outcomes[0].reasons)
    assert "£38,700" not in (res.answer or "")

    # S$ vs US$ / CAD vs USD
    assert passage_contains_literal("Qualifying salary is S$5,000 per month.", "US$5,000 per month") is False
    assert passage_contains_literal("Settlement funds CAD $14,690.", "USD 14,690") is False

    # Bare '$' is ambiguous and must NOT verify an explicit CAD or USD claim
    assert passage_contains_literal("Settlement funds $14,690.", "CAD 14,690") is False
    assert passage_contains_literal("Settlement funds $14,690.", "USD 14,690") is False

    # Bare 'kr' and '¥' must NOT auto-verify SEK or JPY
    assert passage_contains_literal("Synthetic fee is 5,000 kr.", "5,000 SEK") is False
    assert passage_contains_literal("Synthetic fee is ¥50,000.", "JPY 50,000") is False

    # Substring word traps like "fraud 5" or "entrepreneur 5" must NOT fabricate AUD / EUR
    qs_trap = extract_numeric_quantities("An entrepreneur 5 or fraud 5 notice.", include_years=False)
    assert all(q.currency_code is None for q in qs_trap)


def test_7_missing_currency_rejected_conservatively():
    ev_no_cur = _ev("Synthetic route threshold is 38,700 per year.")
    claim_with_cur = AnswerClaim(
        claim="Synthetic route threshold is £38,700 per year.",
        evidence_ids=[ev_no_cur.evidence_id],
    )
    res = verify_material_claims(
        answer=claim_with_cur.claim,
        claims=[claim_with_cur],
        evidence=[ev_no_cur],
        jurisdiction="GB",
        pathway_id=None,
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
    assert "missing_currency_in_evidence" in res.outcomes[0].reasons
    assert res.outcomes[0].retained is False

    # Conversely, stripping currency from a currency-tagged passage fails verification
    ev_cur = _ev("Synthetic route threshold is £38,700 per year.")
    assert claim_numeric_literals_supported("Synthetic route threshold is 38,700 per year.", [ev_cur.passage_text]) is False


# ---------------------------------------------------------------------------
# Tests 8–10: Unit, period, duration, and percentage validation
# ---------------------------------------------------------------------------


def test_8_monthly_vs_annual_mismatch_and_no_auto_annualization():
    ev_mo = _ev("Synthetic route: the monthly salary criterion is €2,801.", jur="NL")
    # Matching monthly claim passes
    c_ok = AnswerClaim(
        claim="For the synthetic route, the monthly salary criterion is €2,801.",
        evidence_ids=[ev_mo.evidence_id],
    )
    res_ok = verify_material_claims(
        answer=c_ok.claim, claims=[c_ok], evidence=[ev_mo], jurisdiction="NL", pathway_id=None
    )
    assert res_ok.outcomes[0].status == ClaimVerificationStatus.SUPPORTED

    # Monthly evidence must NEVER verify annual claim with same digits
    c_yr = AnswerClaim(
        claim="For the synthetic route, the salary criterion is €2,801 per year.",
        evidence_ids=[ev_mo.evidence_id],
    )
    res_yr = verify_material_claims(
        answer=c_yr.claim, claims=[c_yr], evidence=[ev_mo], jurisdiction="NL", pathway_id=None
    )
    assert res_yr.outcomes[0].status == ClaimVerificationStatus.CONTRADICTED
    assert any("unit_period_mismatch" in r for r in res_yr.outcomes[0].reasons)
    assert "2,801" not in (res_yr.answer or "")

    # Auto-annualized 2801 * 12 = 33,612 must NOT verify
    c_ann = AnswerClaim(
        claim="For the synthetic route, the annual salary criterion is €33,612 per year.",
        evidence_ids=[ev_mo.evidence_id],
    )
    res_ann = verify_material_claims(
        answer=c_ann.claim, claims=[c_ann], evidence=[ev_mo], jurisdiction="NL", pathway_id=None
    )
    assert res_ann.outcomes[0].retained is False
    assert "33,612" not in (res_ann.answer or "")

    # Omitting "monthly" on a monthly rate is also rejected (no silent confusion with annual)
    assert claim_numeric_literals_supported("The synthetic salary criterion is €2,801.", [ev_mo.passage_text]) is False


def test_9_hourly_vs_yearly_durations_and_one_time_fees():
    assert passage_contains_literal("Synthetic minimum pay is £18.50 per hour.", "£18.50 per year") is False
    assert passage_contains_literal("Synthetic minimum pay is £18.50 per hour.", "£18.50 per week") is False
    assert passage_contains_literal("Synthetic minimum pay is £18.50 per hour.", "£18.50 per hour") is True

    assert passage_contains_literal("Term-time work is limited to 20 hours per week.", "20 hours per day") is False
    assert passage_contains_literal("Term-time work is limited to 20 hours per week.", "20 days per week") is False
    assert passage_contains_literal("Term-time work is limited to 20 hours weekly.", "20 hours per week") is True

    assert passage_contains_literal("Synthetic visa granted for 3 years.", "3 months") is False
    assert passage_contains_literal("Synthetic visa granted for a 3-year period.", "3 years") is True
    assert passage_contains_literal("One-time synthetic application fee is £719.", "£719 per year") is False

    # End-to-end verification rejects hourly claimed as yearly
    ev_hr = _ev("Synthetic hourly going rate is £18.50 per hour.")
    c_hr_as_yr = AnswerClaim(
        claim="Synthetic hourly going rate is £18.50 per year.",
        evidence_ids=[ev_hr.evidence_id],
    )
    res = verify_material_claims(
        answer=c_hr_as_yr.claim,
        claims=[c_hr_as_yr],
        evidence=[ev_hr],
        jurisdiction="GB",
        pathway_id=None,
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.CONTRADICTED
    assert "18.50" not in (res.answer or "")


def test_10_percentage_comparisons():
    assert passage_contains_literal("Synthetic discount is 10%.", "10 percent") is True
    assert passage_contains_literal("Synthetic discount is 10,5%.", "10.5%") is True
    assert passage_contains_literal("Synthetic discount is 10%.", "10") is False
    assert passage_contains_literal("Synthetic fee is £10.", "10%") is False
    assert passage_contains_literal("Synthetic discount is 10%.", "£10") is False


# ---------------------------------------------------------------------------
# Tests 11–13: Negatives, Decimal precision, and Curated LegalRule
# ---------------------------------------------------------------------------


def test_11_negative_decimals_and_range_dashes():
    qn = parse_single_quantity("-£200.50")
    assert qn is not None and qn.value == Decimal("-200.50") and qn.currency_code == "GBP"

    qp = parse_single_quantity("-5.5%")
    assert qp is not None and qp.value == Decimal("-5.5") and qp.unit == NumericPeriodUnit.PERCENT

    qe = parse_single_quantity("€ -423,00")
    assert qe is not None and qe.value == Decimal("-423.00") and qe.currency_code == "EUR"

    # Range dash "£31,300 - £41,700" must yield two positive amounts, NOT -£41,700!
    rng = extract_numeric_quantities("Salary range is £31,300 - £41,700 per year.")
    assert [q.value for q in rng] == [Decimal("31300"), Decimal("41700")]


def test_12_decimal_precision_no_float_rounding():
    # Binary float 0.1 + 0.2 fails exact comparison; Decimal preserves 4 d.p. exactly
    ev = _ev("Synthetic precision threshold is £41,700.1234 per year.")
    assert passage_contains_literal(ev.passage_text, "£41,700.1234 per year") is True
    assert passage_contains_literal(ev.passage_text, "£41,700.12 per year") is False
    assert passage_contains_literal(ev.passage_text, "£41,700.1235 per year") is False


def test_13_curated_legal_rule_preserves_decimal_currency_and_unit():
    now = datetime.now(timezone.utc)
    rule = SimpleNamespace(
        id=42,
        rule_key="syn_gb.decimal_precision_rule",
        rule_revision=1,
        jurisdiction_code="GB",
        pathway_id="gb_skilled_worker",
        requirement_category="financial_or_sponsorship",
        requirement_type="salary_threshold",
        numeric_value=Decimal("38700.5000"),
        numeric_max_value=None,
        numeric_comparator="gte",
        currency_code="GBP",
        amount_unit="yearly",
        effective_from=date(2025, 1, 1),
        effective_to=None,
        effective_from_inclusive=True,
        effective_to_inclusive=True,
        effective_date_status="known_open_ended",
        cohort_key="general",
        cohort_category="general",
        is_transitional=False,
        conditions_text=None,
        supporting_excerpt=None,
        rule_summary="Synthetic precision Skilled Worker threshold.",
        source_url="https://www.gov.uk/skilled-worker-visa",
        document_id=1,
        document_version_id=2,
        source_id=3,
        review_status="approved",
        verification_status="human_verified",
        reviewed_by="reviewer",
        reviewed_at=now,
    )
    ev_rule = evidence_from_legal_rule(rule)
    assert ev_rule.provenance["numeric_value"] == "38700.5000"
    assert ev_rule.provenance["currency_code"] == "GBP"
    assert ev_rule.provenance["amount_unit"] == "yearly"

    c_exact = AnswerClaim(
        claim="Synthetic precision Skilled Worker threshold is £38,700.50 per year.",
        evidence_ids=[ev_rule.evidence_id],
    )
    res_ok = verify_material_claims(
        answer=c_exact.claim,
        claims=[c_exact],
        evidence=[ev_rule],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        resolved_reference_date=date(2026, 11, 18),
    )
    assert res_ok.outcomes[0].status == ClaimVerificationStatus.SUPPORTED

    # Wrong currency or wrong period against LegalRule is rejected
    c_bad_cur = AnswerClaim(
        claim="Synthetic precision Skilled Worker threshold is €38,700.50 per year.",
        evidence_ids=[ev_rule.evidence_id],
    )
    res_cur = verify_material_claims(
        answer=c_bad_cur.claim,
        claims=[c_bad_cur],
        evidence=[ev_rule],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        resolved_reference_date=date(2026, 11, 18),
    )
    assert res_cur.outcomes[0].status == ClaimVerificationStatus.CONTRADICTED

    c_bad_unit = AnswerClaim(
        claim="Synthetic precision Skilled Worker threshold is £38,700.50 per month.",
        evidence_ids=[ev_rule.evidence_id],
    )
    res_unit = verify_material_claims(
        answer=c_bad_unit.claim,
        claims=[c_bad_unit],
        evidence=[ev_rule],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        resolved_reference_date=date(2026, 11, 18),
    )
    assert res_unit.outcomes[0].status == ClaimVerificationStatus.CONTRADICTED


# ---------------------------------------------------------------------------
# Tests 14–16: Contradictions, legally different matches, prose leak guard
# ---------------------------------------------------------------------------


def test_14_genuine_amount_currency_and_unit_contradictions():
    # €423,00 vs €42,300 is a genuine 100x contradiction
    ev_423 = _ev("Synthetic NL permit fee is €423,00.", jur="NL")
    c_42300 = AnswerClaim(
        claim="Synthetic NL permit fee is €42,300.",
        evidence_ids=[ev_423.evidence_id],
    )
    res = verify_material_claims(
        answer=c_42300.claim, claims=[c_42300], evidence=[ev_423], jurisdiction="NL", pathway_id=None
    )
    assert res.outcomes[0].retained is False
    assert "42,300" not in (res.answer or "")

    # Orchestration _numeric_conflict_warnings: equivalent EU vs Anglo -> NO conflict; different currency/unit -> conflict
    ev_int_eu = _ev("Synthetic NL fee is € 423,00.", jur="NL")
    ev_ext_en = evidence_from_external_item(
        jurisdiction="NL",
        official_url="https://ind.nl/fees",
        provider="national_official",
        passage_text="Synthetic NL fee is €423.00.",
    )
    assert _numeric_conflict_warnings([ev_int_eu, ev_ext_en]) == []

    ev_ext_gbp = evidence_from_external_item(
        jurisdiction="NL",
        official_url="https://ind.nl/fees-other",
        provider="national_official",
        passage_text="Synthetic NL fee is £423.00.",
    )
    assert any("possible_internal_external_numeric_conflict" in w for w in _numeric_conflict_warnings([ev_int_eu, ev_ext_gbp]))


def test_15_numerically_identical_but_legally_different_claims_blocked():
    # Matching figure €38,000 on GEP must not verify CSEP; matching number without lexical overlap fails
    gep = _ev(
        "General Employment Permit remuneration minimum is €38,000 per year.",
        jur="IE",
        url="https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/permit-types/general-employment-permit/",
    )
    c_wrong_path = AnswerClaim(
        claim="Critical Skills Employment Permit remuneration minimum is €38,000 per year.",
        evidence_ids=[gep.evidence_id],
    )
    res_p = verify_material_claims(
        answer=c_wrong_path.claim,
        claims=[c_wrong_path],
        evidence=[gep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
    )
    assert res_p.outcomes[0].status == ClaimVerificationStatus.PATHWAY_MISMATCH
    assert "38,000" not in (res_p.answer or "")

    # Matching number in unrelated topic passage must not verify without overlap
    ev_unrel = _ev("Synthetic administrative penalty fee is £400.")
    c_unrel = AnswerClaim(
        claim="Monthly biometric healthcare surcharge contribution is £400.",
        evidence_ids=[ev_unrel.evidence_id],
    )
    res_u = verify_material_claims(
        answer=c_unrel.claim, claims=[c_unrel], evidence=[ev_unrel], jurisdiction="GB", pathway_id=None
    )
    assert res_u.outcomes[0].retained is False
    assert "400" not in (res_u.answer or "")


def test_16_rejected_numeric_formats_never_leak_into_prose():
    ev = _ev("Synthetic valid UK fee is £38,700 per year.", jur="GB")
    good = AnswerClaim(
        claim="Synthetic valid UK fee is £38,700 per year.",
        evidence_ids=[ev.evidence_id],
    )
    bad_eu = AnswerClaim(
        claim="Synthetic UK processing surcharge is €423,00.",
        evidence_ids=[ev.evidence_id],
    )
    bad_cur = AnswerClaim(
        claim="Synthetic UK fee is €38,700 per year.",
        evidence_ids=[ev.evidence_id],
    )
    bad_amb = AnswerClaim(
        claim="Synthetic UK threshold is 38.000.",
        evidence_ids=[ev.evidence_id],
    )
    raw_prose = (
        "Synthetic valid UK fee is £38,700 per year. "
        "Synthetic UK processing surcharge is €423,00. "
        "Synthetic UK fee is €38,700 per year. "
        "Synthetic UK threshold is 38.000."
    )
    grounding = validate_and_ground_answer(
        GroundedAnswer(answer=raw_prose, claims=[good, bad_eu, bad_cur, bad_amb]),
        evidence=[ev],
        jurisdiction="GB",
    )
    assert len(grounding.answer.claims) == 1
    assert "£38,700" in grounding.answer.answer
    assert "423,00" not in grounding.answer.answer
    assert "€38,700" not in grounding.answer.answer
    assert "38.000" not in grounding.answer.answer

    ver = verify_material_claims(
        answer=raw_prose,
        claims=[good, bad_eu, bad_cur, bad_amb],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id=None,
    )
    assert len(ver.claims) == 1
    assert "£38,700" in (ver.answer or "")
    assert "423,00" not in (ver.answer or "")
    assert "€38,700" not in (ver.answer or "")
    assert "38.000" not in (ver.answer or "")


# ---------------------------------------------------------------------------
# Tests 17–20: X01/X16, X17 decimal-comma, temporal compatibility, API + perf
# ---------------------------------------------------------------------------


def test_17_and_18_x01_x16_transitional_and_x17_decimal_comma_regressions():
    # X01/X16 transitional + general with decimal/currency/unit checks intact
    ev_sw = _ev(
        "Skilled Worker visa: the general salary threshold is £41,700 per year. "
        "For those who held a Tier 2 or Skilled Worker visa before 4 April 2024, "
        "the transitional minimum is £31,300 per year and applies only to applications made before 4 April 2030.",
        url="https://www.gov.uk/skilled-worker-visa",
    )
    c1 = AnswerClaim(
        claim="The Skilled Worker general salary threshold is £41,700 per year.",
        evidence_ids=[ev_sw.evidence_id],
    )
    c2 = AnswerClaim(
        claim="For pre-4 April 2024 Skilled Worker visa holders, the transitional minimum is £31,300 per year and applies only to applications made before 4 April 2030.",
        evidence_ids=[ev_sw.evidence_id],
    )
    res_sw = verify_material_claims(
        answer=f"{c1.claim} {c2.claim}",
        claims=[c1, c2],
        evidence=[ev_sw],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert [o.status for o in res_sw.outcomes] == [
        ClaimVerificationStatus.SUPPORTED,
        ClaimVerificationStatus.SUPPORTED,
    ]

    # X17 decimal-comma regression: IND fees € 423,00 and € 85,00 (NEVER 42,300 or 8,500)
    ev_ind = _ev(
        "IND Fees: cross-border service provider permit fee is € 85,00; "
        "seafaring and international trade regulation permit fee is € 423,00.",
        jur="NL",
        url="https://ind.nl/en/fees-costs-of-an-application",
    )
    c_ind = AnswerClaim(
        claim="IND states €85.00 for cross-border service provider permits and €423.00 for seafaring regulation permits.",
        evidence_ids=[ev_ind.evidence_id],
    )
    res_ind = verify_material_claims(
        answer=c_ind.claim,
        claims=[c_ind],
        evidence=[ev_ind],
        jurisdiction="NL",
        pathway_id="nl_fees_hub",
    )
    assert res_ind.outcomes[0].status == ClaimVerificationStatus.SUPPORTED

    # Corrupted 42,300 claim against € 423,00 passage MUST fail
    c_ind_bad = AnswerClaim(
        claim="IND states €42,300 for seafaring regulation permits.",
        evidence_ids=[ev_ind.evidence_id],
    )
    res_ind_bad = verify_material_claims(
        answer=c_ind_bad.claim,
        claims=[c_ind_bad],
        evidence=[ev_ind],
        jurisdiction="NL",
        pathway_id="nl_fees_hub",
    )
    assert res_ind_bad.outcomes[0].retained is False
    assert "42,300" not in (res_ind_bad.answer or "")


def test_19_historical_and_future_numerical_verification():
    ev_hist = _ev(
        "Previously in 2020 the synthetic EU permit fee was € 423,00.",
        jur="NL",
        legal_status="historical",
    )
    c_hist = AnswerClaim(
        claim="Previously in 2020 the synthetic EU permit fee was €423.00.",
        evidence_ids=[ev_hist.evidence_id],
    )
    res_hist = verify_material_claims(
        answer=c_hist.claim,
        claims=[c_hist],
        evidence=[ev_hist],
        jurisdiction="NL",
        pathway_id=None,
        resolved_reference_date=date(2020, 6, 1),
        temporal_mode="historical",
    )
    assert res_hist.outcomes[0].status == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY
    assert "€423.00" in (res_hist.answer or "")

    # Future rule with currency & unit mismatch must still be caught cleanly
    ev_fut = _ev(
        "Effective from 1 December 2026, the future threshold will be £52,000 per year.",
        legal_status="future_pending",
    )
    c_fut_bad_unit = AnswerClaim(
        claim="Effective from 1 December 2026, the future threshold will be £52,000 per month.",
        evidence_ids=[ev_fut.evidence_id],
    )
    res_fut = verify_material_claims(
        answer=c_fut_bad_unit.claim,
        claims=[c_fut_bad_unit],
        evidence=[ev_fut],
        jurisdiction="GB",
        pathway_id=None,
        resolved_reference_date=date(2026, 11, 18),
        temporal_mode="current",
    )
    assert res_fut.outcomes[0].status == ClaimVerificationStatus.CONTRADICTED
    assert "52,000" not in (res_fut.answer or "")


def test_20_api_compatibility_zero_extra_llm_calls_and_perf(reset_agent_runtime, monkeypatch):
    import lifepilot.api.app as api_app

    ev_nl = _ev(
        "IND synthetic fee guidance: permit application costs € 423,00 per application.",
        jur="NL",
        url="https://ind.nl/en/fees",
    )

    class _MockOrch:
        def gather(self, **_kwargs):
            return OrchestrationResult(
                evidence=[ev_nl],
                status=AgentStatus.SUCCESS,
                internal_status="ok",
                resolved_reference_date="2026-11-18",
                temporal_mode="current",
                temporal_outcome="current_candidate_unverified",
            )

    set_evidence_orchestrator_override(_MockOrch())
    llm = _SingleCallFakeLLM(
        lambda _p: GroundedAnswer(
            answer="The synthetic IND application costs €423.00 per application.",
            claims=[
                AnswerClaim(
                    claim="IND synthetic fee guidance states the permit application costs €423.00 per application.",
                    evidence_ids=[ev_nl.evidence_id],
                )
            ],
        )
    )
    set_llm_provider_override(llm)

    monkeypatch.setenv("LIFEPILOT_ENV", "development")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_AUTH", "1")
    monkeypatch.setenv("LIFEPILOT_API_KEYS", "c1-key")
    monkeypatch.setenv("LIFEPILOT_VERIFICATION_ENABLED", "1")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_LLM", "0")
    reset_api_settings_cache()
    reset_agent_config_cache()
    reset_rate_limit_state()

    client = TestClient(api_app.create_app())
    r = client.post(
        "/v1/answer",
        headers={"X-API-Key": "c1-key"},
        json={"question": "What is the synthetic IND permit fee?", "jurisdiction": "NL"},
    )
    assert r.status_code == 200
    body = r.json()
    assert llm.calls == 1
    assert body["status"] == "success"
    assert "423.00" in (body["answer"] or "")
    assert body["verification"]["claim_outcomes"][0]["status"] == "supported"

    # Zero-evidence insufficient path triggers 0 LLM calls
    class _EmptyOrch:
        def gather(self, **_kwargs):
            return OrchestrationResult(
                evidence=[],
                status=AgentStatus.INSUFFICIENT_EVIDENCE,
                missing_information=["retrieved_evidence"],
            )

    set_evidence_orchestrator_override(_EmptyOrch())
    llm_zero = _SingleCallFakeLLM(
        lambda _p: (_ for _ in ()).throw(AssertionError("LLM must not run"))
    )
    set_llm_provider_override(llm_zero)
    resp_empty = run_agent(
        AgentRequest(question="What is the fee?", jurisdiction="NL"),
        config=AgentConfig(llm_provider="fake", llm_model="fake", verification_enabled=True),
    )
    assert llm_zero.calls == 0
    assert resp_empty.status == AgentStatus.INSUFFICIENT_EVIDENCE

    # Performance sanity check: 200 numeric verifications complete well under 0.5s
    t0 = time.perf_counter()
    for _ in range(200):
        diagnose_claim_numeric_support(
            "The threshold is €4.238,00 per year with a 10.5% discount.",
            ["Official rule: €4,238.00 per year and 10,5% discount."],
        )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 500.0, f"Numeric verification too slow: {elapsed_ms:.1f}ms"
