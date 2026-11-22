"""Phase 10B.4 regression tests: temporal verification & transitional rules.

Uses synthetic fixtures for new rules and preserves X01/X16 transitional regression
assertions against captured wording. Zero live DB writes; zero paid LLM calls.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from lifepilot.agent.answer import AnswerClaim, GroundedAnswer
from lifepilot.agent.config import AgentConfig
from lifepilot.agent.contracts import AgentRequest, AgentStatus
from lifepilot.agent.evidence import (
    EvidenceItem,
    evidence_from_external_item,
    evidence_from_internal_passage,
    evidence_from_legal_rule,
)
from lifepilot.agent.grounding import (
    compose_grounded_answer_text,
    validate_and_ground_answer,
)
from lifepilot.agent.orchestration import (
    EvidenceOrchestrator,
    set_evidence_orchestrator_override,
)
from lifepilot.agent.providers import (
    LLMUsage,
    StructuredLLMResult,
    set_llm_provider_override,
)
from lifepilot.agent.retrieval_policy import reconcile_effective_dates
from lifepilot.agent.runner import run_agent
from lifepilot.agent.temporal_dates import (
    detect_question_temporal_intent,
    extract_effective_date_mentions,
    parse_unambiguous_date_literal,
)
from lifepilot.agent.temporal_selection import TemporalQueryMode
from lifepilot.agent.verification import (
    ClaimVerificationStatus,
    verify_material_claims,
)
from lifepilot.db.models import (
    PLANNED_EMBEDDING_MODEL_ID,
    Authority,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    DocumentVersion,
    Jurisdiction,
    LegalRule,
    Source,
)
from lifepilot.rag.embedder import FakeEmbedder


def _sha(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


def _ev(
    *,
    text: str,
    url: str = "https://example.test/gb/alpha-route",
    jur: str = "GB",
    chunk_id: int = 1,
    legal_status: str = "unknown",
    is_current_version: bool = True,
) -> EvidenceItem:
    return evidence_from_internal_passage(
        jurisdiction=jur,
        official_url=url,
        document_id=100 + chunk_id,
        document_version_id=200 + chunk_id,
        chunk_id=chunk_id,
        content_text=text,
        score=0.9,
        legal_status=legal_status,
        is_current_version=is_current_version,
    )


class _CountingFakeLLM:
    provider_id = "fake"
    model_id = "fake-claude"

    def __init__(self, answer: str, claims: list[AnswerClaim]):
        self.answer = answer
        self.claims = claims
        self.calls = 0

    def complete(self, *a, **k):
        raise AssertionError("complete() must not be called")

    def complete_structured(self, messages, *, schema, max_tokens, temperature=0.0):
        self.calls += 1
        return StructuredLLMResult(
            value=GroundedAnswer(
                answer=self.answer,
                claims=self.claims,
                missing_information=[],
                limitations=[],
            ),
            usage=LLMUsage(input_tokens=20, output_tokens=30, total_tokens=50),
            latency_ms=4.0,
            model=self.model_id,
            provider=self.provider_id,
        )


def _seed_rule_graph(session: Session, *, code: str = "GB", text_val: str = "Synthetic text.") -> dict:
    jur = session.query(Jurisdiction).filter_by(code=code).first()
    if jur is None:
        jur = Jurisdiction(
            code=code, name=f"Country {code}", jurisdiction_type="country", country_code=code
        )
        session.add(jur)
        session.flush()
    auth = Authority(jurisdiction_id=jur.id, name=f"Auth-{code}-{_sha(text_val)[:6]}")
    session.add(auth)
    session.flush()
    src = Source(
        authority_id=auth.id,
        jurisdiction_id=jur.id,
        name=f"Src-{code}-{_sha(text_val)[:6]}",
        seed_url=f"https://example.test/{code.lower()}/{_sha(text_val)[:8]}",
        seed_url_normalized=f"https://example.test/{code.lower()}/{_sha(text_val)[:8]}",
        review_status="approved",
    )
    session.add(src)
    session.flush()
    doc = Document(
        authority_id=auth.id,
        primary_source_id=src.id,
        canonical_url=f"https://example.test/{code.lower()}/route-{_sha(text_val)[:8]}",
        url_normalized=f"https://example.test/{code.lower()}/route-{_sha(text_val)[:8]}",
        content_kind="html_page",
        language="en",
        discovery_status="seed",
    )
    session.add(doc)
    session.flush()
    ver = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=src.id,
        version_number=1,
        source_url=doc.canonical_url,
        content_type="text/html",
        retrieved_at=datetime.now(timezone.utc),
        http_status=200,
        content_sha256=_sha(text_val),
        byte_size=len(text_val),
        storage_path=f"raw/documents/{doc.id}/v1.html",
        is_current=True,
    )
    session.add(ver)
    session.flush()
    return {"jur": jur, "auth": auth, "src": src, "doc": doc, "ver": ver}


def _make_rule(g: dict, **overrides) -> LegalRule:
    payload = {
        "rule_key": "xx_route.synthetic",
        "rule_revision": 1,
        "jurisdiction_id": g["jur"].id,
        "jurisdiction_code": g["jur"].code,
        "pathway_id": "xx_route",
        "requirement_category": "financial_or_sponsorship",
        "requirement_type": "salary_threshold",
        "numeric_value": Decimal("30000.00"),
        "numeric_comparator": "gte",
        "currency_code": "GBP",
        "amount_unit": "yearly",
        "effective_from": date(2024, 1, 1),
        "effective_to": None,
        "effective_from_inclusive": True,
        "effective_to_inclusive": True,
        "effective_date_status": "known_open_ended",
        "cohort_key": "general",
        "cohort_category": "general",
        "is_transitional": False,
        "cohort_conditions": {},
        "rule_summary": "Synthetic general threshold.",
        "document_id": g["doc"].id,
        "document_version_id": g["ver"].id,
        "source_id": g["src"].id,
        "source_url": g["doc"].canonical_url,
        "review_status": "unreviewed",
        "verification_status": "unverified",
        "reviewed_by": None,
        "reviewed_at": None,
    }
    payload.update(overrides)
    if payload.get("verification_status") == "human_verified":
        payload["verification_status"] = "unverified"
        payload["review_status"] = "unreviewed"
        payload["reviewed_by"] = None
        payload["reviewed_at"] = None
    return LegalRule(**payload)


def _authorize_rules(session: Session, rules: list[LegalRule]) -> list[LegalRule]:
    from lifepilot.governance.synthetic_authority import ensure_synthetic_human_authority

    return [ensure_synthetic_human_authority(session, r) for r in rules]


@pytest.fixture()
def _clean(reset_agent_runtime):
    yield


# ---------------------------------------------------------------------------
# 1–5. Exact dates, same-year future, boundaries, intervals, sunset/expiry
# ---------------------------------------------------------------------------


def test_same_year_future_effective_and_date_formats():
    # Supported exact formats
    assert parse_unambiguous_date_literal("2026-12-01") == date(2026, 12, 1)
    assert parse_unambiguous_date_literal("1 December 2026") == date(2026, 12, 1)
    assert parse_unambiguous_date_literal("December 1, 2026") == date(2026, 12, 1)
    assert parse_unambiguous_date_literal("04/05/2026") is None  # ambiguous

    # Same-year future date (2026-12-01) evaluated from 2026-11-18 -> future!
    ev_same_year = _ev(
        text="Effective from 1 December 2026, the synthetic route threshold will be £52,000."
    )
    notes_nov = reconcile_effective_dates(
        [ev_same_year], reference_date=date(2026, 11, 18)
    )
    assert notes_nov.future_effective_mentions

    claim = AnswerClaim(
        claim="The current synthetic route threshold is £52,000.",
        evidence_ids=[ev_same_year.evidence_id],
    )
    res_nov = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev_same_year],
        jurisdiction="GB",
        pathway_id=None,
        resolved_reference_date=date(2026, 11, 18),
        temporal_mode="current",
    )
    assert res_nov.outcomes[0].status == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY

    # Same passage queried in future mode on 2026-12-02 -> active!
    future_claim = AnswerClaim(
        claim="Effective from 1 December 2026, the synthetic route threshold is £52,000.",
        evidence_ids=[ev_same_year.evidence_id],
    )
    res_dec = verify_material_claims(
        answer=future_claim.claim,
        claims=[future_claim],
        evidence=[ev_same_year],
        jurisdiction="GB",
        pathway_id=None,
        resolved_reference_date=date(2026, 12, 2),
        temporal_mode="future",
    )
    assert res_dec.outcomes[0].status == ClaimVerificationStatus.SUPPORTED


def test_inclusive_exclusive_boundaries_and_intervals():
    text = (
        "Valid from 2026-01-01 to 2026-06-30. "
        "Transitional fee applies after 2026-07-01 and before 2026-12-31."
    )
    m_on_end = extract_effective_date_mentions(text, reference_date=date(2026, 6, 30))
    assert not any(m.is_expired_for_reference for m in m_on_end if m.date_literal == "2026-06-30")

    m_excl_end = extract_effective_date_mentions(text, reference_date=date(2026, 12, 31))
    assert any(
        m.date_literal == "2026-12-31" and m.inclusive is False and m.is_expired_for_reference
        for m in m_excl_end
    )

    m_excl_start = extract_effective_date_mentions(text, reference_date=date(2026, 7, 1))
    assert any(
        m.date_literal == "2026-07-01" and m.inclusive is False and m.is_future_for_reference
        for m in m_excl_start
    )


def test_sunset_and_expiry_dates_against_reference_date():
    ev_expired = _ev(
        text="Until 31 March 2026, the synthetic registration fee was £400 for eligible applicants."
    )
    claim_present = AnswerClaim(
        claim="The synthetic registration fee is £400 for eligible applicants.",
        evidence_ids=[ev_expired.evidence_id],
    )
    res_expired = verify_material_claims(
        answer=claim_present.claim,
        claims=[claim_present],
        evidence=[ev_expired],
        jurisdiction="GB",
        pathway_id=None,
        resolved_reference_date=date(2026, 11, 18),
        temporal_mode="current",
    )
    assert res_expired.outcomes[0].status == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY

    # Active future sunset (before 4 April 2030) on 2026-11-18 is NOT expired.
    ev_active_sunset = _ev(
        text="Transitional synthetic fee of £400 applies before 4 April 2030."
    )
    claim_active = AnswerClaim(
        claim="Transitional synthetic fee of £400 applies before 4 April 2030.",
        evidence_ids=[ev_active_sunset.evidence_id],
    )
    res_active = verify_material_claims(
        answer=claim_active.claim,
        claims=[claim_active],
        evidence=[ev_active_sunset],
        jurisdiction="GB",
        pathway_id=None,
        resolved_reference_date=date(2026, 11, 18),
        temporal_mode="current",
    )
    assert res_active.outcomes[0].status == ClaimVerificationStatus.SUPPORTED


# ---------------------------------------------------------------------------
# 6–9. Transitional X01/X16 regression, compatible cohorts, same-scope conflicts
# ---------------------------------------------------------------------------


def test_x01_x16_transitional_claim_not_suppressed_by_general_threshold():
    """Exact X01/X16 regression: transitional 'only before 4 April 2030' coexists with £41,700."""
    ev_main = _ev(
        text=(
            "Skilled Worker visa: the general salary threshold is £41,700 per year "
            "or the going rate for the occupation."
        ),
        url="https://www.gov.uk/skilled-worker-visa",
        chunk_id=1,
    )
    ev_trans = _ev(
        text=(
            "If you got your certificate of sponsorship for your first Tier 2 or "
            "Skilled Worker visa before 4 April 2024 and have continually held "
            "Skilled Worker visas since, you can be paid the higher of £31,300 or "
            "the lower going rate when extending or updating. These lower "
            "requirements apply only to applications made before 4 April 2030."
        ),
        url="https://www.gov.uk/skilled-worker-visa/certificate-of-sponsorship-before-4-april-2024",
        chunk_id=2,
    )

    c_general = AnswerClaim(
        claim="The Skilled Worker general salary threshold is £41,700 per year or the going rate.",
        evidence_ids=[ev_main.evidence_id],
    )
    c_trans = AnswerClaim(
        claim=(
            "For those who held a Tier 2 or Skilled Worker visa before 4 April 2024 "
            "and have continually held Skilled Worker visas since, the required pay "
            "is the higher of £31,300 or the lower going rate for the occupation when "
            "extending or updating the visa; these lower requirements apply only to "
            "applications made before 4 April 2030."
        ),
        evidence_ids=[ev_trans.evidence_id],
    )

    res = verify_material_claims(
        answer=f"{c_general.claim} {c_trans.claim}",
        claims=[c_general, c_trans],
        evidence=[ev_main, ev_trans],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        question="How do going rates interact with the Skilled Worker general salary threshold?",
        resolved_reference_date=date(2026, 11, 18),
        temporal_mode="current",
    )
    assert [o.status for o in res.outcomes] == [
        ClaimVerificationStatus.SUPPORTED,
        ClaimVerificationStatus.SUPPORTED,
    ]
    assert all(o.retained for o in res.outcomes)
    assert "£31,300" in (res.answer or "")
    assert "£41,700" in (res.answer or "")
    assert "before 4 April 2030" in (res.answer or "")


def test_different_cohorts_compatible_vs_same_cohort_contradiction():
    ev_gen = _ev(
        text="Alpha route general threshold is £45,000 per year.",
        url="https://example.test/gb/alpha-route",
        chunk_id=1,
    )
    ev_phd = _ev(
        text="Alpha route STEM PhD applicants may qualify with at least £36,000 per year.",
        url="https://example.test/gb/alpha-route-phd",
        chunk_id=2,
    )
    ev_bad = _ev(
        text="Alpha route general threshold is exactly £39,000 only; £45,000 does not apply.",
        url="https://example.test/gb/alpha-route-conflict",
        chunk_id=3,
    )

    c_phd = AnswerClaim(
        claim="For STEM PhD applicants, the Alpha route threshold is £36,000 per year.",
        evidence_ids=[ev_phd.evidence_id],
    )
    c_gen_excl = AnswerClaim(
        claim="The Alpha route general threshold is only exactly £45,000 per year.",
        evidence_ids=[ev_gen.evidence_id],
    )

    # Different cohorts without conflict
    res_compat = verify_material_claims(
        answer=c_phd.claim,
        claims=[c_phd],
        evidence=[ev_gen, ev_phd],
        jurisdiction="GB",
        pathway_id=None,
        resolved_reference_date=date(2026, 11, 18),
    )
    assert res_compat.outcomes[0].status == ClaimVerificationStatus.SUPPORTED

    # Same cohort unconditional contradiction still detected
    res_contra = verify_material_claims(
        answer=c_gen_excl.claim,
        claims=[c_gen_excl],
        evidence=[ev_gen, ev_bad],
        jurisdiction="GB",
        pathway_id=None,
        resolved_reference_date=date(2026, 11, 18),
    )
    assert res_contra.outcomes[0].status == ClaimVerificationStatus.CONTRADICTED
    assert res_contra.outcomes[0].retained is False


# ---------------------------------------------------------------------------
# 10–14. LegalRule integration, missing cohorts, archived & proposed sources
# ---------------------------------------------------------------------------


def test_reviewed_legal_rule_vs_non_applicable_unreviewed_and_missing_cohort(
    db_session: Session,
):
    g = _seed_rule_graph(db_session, code="GB", text_val="Synthetic rule source")

    applicable_rule = _make_rule(
        g,
        rule_key="xx_route.general_2026",
        pathway_id="gb_skilled_worker",
        numeric_value=Decimal("41700.00"),
        effective_from=date(2025, 7, 22),
        effective_to=None,
        effective_date_status="known_open_ended",
        cohort_key="general",
        rule_summary="General synthetic threshold GBP 41,700 from 2025-07-22.",
    )
    expired_rule = _make_rule(
        g,
        rule_key="xx_route.old_2020",
        pathway_id="gb_skilled_worker",
        numeric_value=Decimal("25600.00"),
        effective_from=date(2020, 1, 1),
        effective_to=date(2023, 12, 31),
        effective_date_status="known_bounded",
        cohort_key="general",
        rule_summary="Historical synthetic threshold GBP 25,600 until 2023-12-31.",
    )
    cohort_rule = _make_rule(
        g,
        rule_key="xx_route.transitional_cohort",
        pathway_id="gb_skilled_worker",
        numeric_value=Decimal("31300.00"),
        effective_from=date(2024, 4, 4),
        effective_to=date(2030, 4, 4),
        effective_to_inclusive=False,
        effective_date_status="known_bounded",
        cohort_key="transitional_legacy",
        cohort_category="transitional",
        is_transitional=True,
        rule_summary="Transitional cohort synthetic threshold GBP 31,300 before 2030-04-04.",
    )
    db_session.add_all([applicable_rule, expired_rule, cohort_rule])
    db_session.flush()
    _authorize_rules(db_session, [applicable_rule, expired_rule, cohort_rule])

    ev_app = evidence_from_legal_rule(applicable_rule)
    ev_exp = evidence_from_legal_rule(expired_rule)
    ev_coh = evidence_from_legal_rule(cohort_rule)

    # Unverified passage contradicting applicable reviewed rule is rejected.
    ev_stale_passage = _ev(
        text="Skilled Worker visa general salary threshold is £38,700 per year.",
        url="https://www.gov.uk/skilled-worker-visa",
        chunk_id=9,
    )
    c_bad_unverified = AnswerClaim(
        claim="The Skilled Worker visa general salary threshold is £38,700 per year.",
        evidence_ids=[ev_stale_passage.evidence_id],
    )
    res_override = verify_material_claims(
        answer=c_bad_unverified.claim,
        claims=[c_bad_unverified],
        evidence=[ev_app, ev_stale_passage],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        resolved_reference_date=date(2026, 11, 18),
    )
    assert res_override.outcomes[0].status == ClaimVerificationStatus.CONTRADICTED
    assert "conflicts_with_reviewed_legal_rule" in res_override.outcomes[0].reasons

    # Reviewed but non-applicable (2020-2023) rule on 2026-11-18 is NOT retained as settled fact.
    c_exp_rule = AnswerClaim(
        claim="The synthetic threshold is GBP 25,600.",
        evidence_ids=[ev_exp.evidence_id],
    )
    res_non_app = verify_material_claims(
        answer=c_exp_rule.claim,
        claims=[c_exp_rule],
        evidence=[ev_exp],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        resolved_reference_date=date(2026, 11, 18),
    )
    assert res_non_app.outcomes[0].status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
    assert res_non_app.outcomes[0].retained is False

    # Same historical rule IS supported when queried at 2022-06-01!
    c_hist_ok = AnswerClaim(
        claim="Historical synthetic threshold GBP 25,600 applied until 2023-12-31.",
        evidence_ids=[ev_exp.evidence_id],
    )
    res_hist = verify_material_claims(
        answer=c_hist_ok.claim,
        claims=[c_hist_ok],
        evidence=[ev_exp],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        resolved_reference_date=date(2022, 6, 1),
        temporal_mode="historical",
    )
    assert res_hist.outcomes[0].status == ClaimVerificationStatus.SUPPORTED

    # Cohort-restricted rule when claim omits cohort restriction -> temporal_uncertainty.
    c_omits_cohort = AnswerClaim(
        claim="The general synthetic threshold is GBP 31,300.",
        evidence_ids=[ev_coh.evidence_id],
    )
    res_omitted = verify_material_claims(
        answer=c_omits_cohort.claim,
        claims=[c_omits_cohort],
        evidence=[ev_coh],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        resolved_reference_date=date(2026, 11, 18),
        user_circumstances=None,
    )
    assert res_omitted.outcomes[0].status == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY

    # Unreviewed LegalRule cannot even construct curated_rule evidence.
    unrev = _make_rule(
        g,
        rule_key="xx_route.unrev",
        review_status="unreviewed",
        verification_status="unverified",
        reviewed_by=None,
        reviewed_at=None,
    )
    with pytest.raises(ValueError):
        evidence_from_legal_rule(unrev)


def test_archived_historical_uncertainty_and_proposed_vs_enacted():
    ev_arch = _ev(
        text="Archived 2021 synthetic permit guidance listed a fee of €1,000.",
        legal_status="archived",
        is_current_version=False,
    )
    c_arch = AnswerClaim(
        claim="In 2021 the archived synthetic permit guidance listed a fee of €1,000.",
        evidence_ids=[ev_arch.evidence_id],
    )
    res_arch = verify_material_claims(
        answer=c_arch.claim,
        claims=[c_arch],
        evidence=[ev_arch],
        jurisdiction="GB",
        pathway_id=None,
        resolved_reference_date=date(2021, 6, 1),
        temporal_mode="historical",
    )
    assert res_arch.outcomes[0].status == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY
    assert res_arch.outcomes[0].retained is True

    ev_prop = evidence_from_external_item(
        jurisdiction="US",
        official_url="https://www.federalregister.gov/d/2026-999",
        provider="us_federal_register",
        passage_text="Proposed rule: proposes increasing synthetic filing fee to $999.",
        published_at="2026-10-01",
        effective_at="2027-06-01",
        legal_status="proposed_not_effective",
    )
    c_prop = AnswerClaim(
        claim="The filing fee is $999.",
        evidence_ids=[ev_prop.evidence_id],
    )
    res_prop = verify_material_claims(
        answer=c_prop.claim,
        claims=[c_prop],
        evidence=[ev_prop],
        jurisdiction="US",
        pathway_id=None,
        resolved_reference_date=date(2026, 11, 18),
        temporal_mode="current",
    )
    assert res_prop.outcomes[0].status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
    assert res_prop.outcomes[0].retained is False


# ---------------------------------------------------------------------------
# 15–17. Year-only historical intent, ambiguous dates, explicit override
# ---------------------------------------------------------------------------


def test_year_only_historical_question_does_not_invent_date_and_detects_multi_period(
    db_session: Session, _clean
):
    today = date(2026, 11, 18)
    intent = detect_question_temporal_intent(
        "What was the synthetic threshold in 2020?",
        explicit_as_of_date=None,
        today_utc=today,
    )
    assert intent.temporal_mode == TemporalQueryMode.HISTORICAL
    assert intent.resolved_reference_date is None
    assert intent.period_year == 2020
    assert intent.period_start == date(2020, 1, 1)
    assert intent.period_end == date(2020, 12, 31)

    # Cohort phrasing "CoS before 4 April 2024" stays CURRENT (not historical query).
    cohort_intent = detect_question_temporal_intent(
        "What are current rules if my CoS was issued before 4 April 2024?",
        explicit_as_of_date=None,
        today_utc=today,
    )
    assert cohort_intent.temporal_mode == TemporalQueryMode.CURRENT
    assert cohort_intent.resolved_reference_date == today

    # Explicit as_of_date overrides question date.
    override_intent = detect_question_temporal_intent(
        "What applied in 2020?",
        explicit_as_of_date=date(2023, 5, 10),
        today_utc=today,
    )
    assert override_intent.explicit_override is True
    assert override_intent.resolved_reference_date == date(2023, 5, 10)

    # Seed two rules inside 2020 -> year-only query surfaces multi-period clarification without inventing Jan 1.
    g = _seed_rule_graph(db_session, code="GB", text_val="Multi-period 2020 doc")
    r_h1 = _make_rule(
        g,
        rule_key="xx_2020.h1",
        pathway_id="gb_skilled_worker",
        numeric_value=Decimal("20000.00"),
        effective_from=date(2020, 1, 1),
        effective_to=date(2020, 6, 30),
        effective_date_status="known_bounded",
        rule_summary="First-half 2020 synthetic threshold GBP 20,000.",
    )
    r_h2 = _make_rule(
        g,
        rule_key="xx_2020.h2",
        pathway_id="gb_skilled_worker",
        numeric_value=Decimal("25600.00"),
        effective_from=date(2020, 7, 1),
        effective_to=date(2020, 12, 31),
        effective_date_status="known_bounded",
        rule_summary="Second-half 2020 synthetic threshold GBP 25,600.",
    )
    db_session.add_all([r_h1, r_h2])
    db_session.flush()
    _authorize_rules(db_session, [r_h1, r_h2])

    @contextmanager
    def _bound():
        yield db_session

    orch = EvidenceOrchestrator(session_factory=_bound, embedder=FakeEmbedder())
    orch._retrieve_internal = lambda **_kw: ([], "no_results", 0.5)  # type: ignore[method-assign]
    res = orch.gather(
        question="What was the synthetic threshold in 2020?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.resolved_reference_date is None
    assert res.temporal_mode == "historical"
    assert "exact_as_of_date_within_period:2020" in res.missing_information
    assert any("multiple_legal_periods_in_requested_window" in w for w in res.warnings)


def test_ambiguous_date_in_question_and_passage_never_guessed(_clean):
    amb_intent = detect_question_temporal_intent(
        "What fee applied on 04/05/2022?",
        explicit_as_of_date=None,
        today_utc=date(2026, 11, 18),
    )
    assert amb_intent.clarification_required is True
    assert amb_intent.ambiguous_date_literal == "04/05/2022"
    assert "unambiguous_as_of_date_YYYY_MM_DD" in amb_intent.missing_information

    @contextmanager
    def _dummy():
        yield None

    orch = EvidenceOrchestrator(session_factory=_dummy)
    orch._retrieve_internal = lambda **_kw: (_ for _ in ()).throw(
        AssertionError("should abstain before retrieval on ambiguous question date")
    )  # type: ignore[method-assign]
    res = orch.gather(question="What fee applied on 04/05/2022?", jurisdiction="GB")
    assert res.status == AgentStatus.INSUFFICIENT_EVIDENCE
    assert res.evidence == []
    assert "unambiguous_as_of_date_YYYY_MM_DD" in res.missing_information

    # Ambiguous date inside passage triggers TEMPORAL_UNCERTAINTY
    ev_amb = _ev(text="The new synthetic threshold applies from 04/05/2026 and is £45,000.")
    c_amb = AnswerClaim(
        claim="The synthetic threshold is £45,000.",
        evidence_ids=[ev_amb.evidence_id],
    )
    ver = verify_material_claims(
        answer=c_amb.claim,
        claims=[c_amb],
        evidence=[ev_amb],
        jurisdiction="GB",
        pathway_id=None,
        resolved_reference_date=date(2026, 11, 18),
    )
    assert ver.outcomes[0].status == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY


# ---------------------------------------------------------------------------
# 18–20. Rejected-claim prose leakage, pathway grounding, and LLM call count
# ---------------------------------------------------------------------------


def test_rejected_temporal_claims_and_dropped_qualifiers_never_leak_into_prose():
    # Case A: suppressed figure must not survive via raw summary prose.
    kept = [
        AnswerClaim(
            claim="The transitional threshold is £31,300 only for applications made before 4 April 2030.",
            evidence_ids=["ev_12345678"],
            uncertainty="Applies to legacy cohort only.",
        )
    ]
    # Raw prose tries to drop transitional qualifier AND reintroduce suppressed £99,999.
    rebuilt, was_rebuilt = compose_grounded_answer_text(
        "The threshold is £31,300. Another source said £99,999.",
        kept,
        suppressed_claims=["Another source said £99,999."],
    )
    assert was_rebuilt is True
    assert "99,999" not in rebuilt
    assert "before 4 April 2030" in rebuilt
    assert "Applies to legacy cohort only" in rebuilt


def test_e2e_single_llm_call_retains_transitional_rule_and_preserves_grounding(
    db_session: Session, _clean
):
    fake_emb = FakeEmbedder()
    ev_gen = _ev(
        text="Skilled Worker visa: the general salary threshold is £41,700 per year.",
        url="https://www.gov.uk/skilled-worker-visa",
        chunk_id=1,
    )
    ev_trans = _ev(
        text=(
            "If you held a Tier 2 or Skilled Worker visa before 4 April 2024 and "
            "continually held Skilled Worker visas since, the minimum salary is "
            "£31,300; these lower requirements apply only to applications made "
            "before 4 April 2030."
        ),
        url="https://www.gov.uk/skilled-worker-visa/certificate-of-sponsorship-before-4-april-2024",
        chunk_id=2,
    )

    class _FakeOrch:
        def gather(self, **_kw):
            from lifepilot.agent.orchestration import OrchestrationResult

            return OrchestrationResult(
                evidence=[ev_gen, ev_trans],
                status=AgentStatus.SUCCESS,
                resolved_reference_date="2026-11-18",
                temporal_mode="current",
                temporal_outcome="current_candidate_unverified",
            )

    llm = _CountingFakeLLM(
        answer=(
            "The general salary threshold is £41,700 per year. "
            "For pre-4 April 2024 Tier 2 or Skilled Worker holders continually holding "
            "visas, the transitional minimum salary is £31,300 and applies only before "
            "4 April 2030."
        ),
        claims=[
            AnswerClaim(
                claim="The Skilled Worker general salary threshold is £41,700 per year.",
                evidence_ids=[ev_gen.evidence_id],
            ),
            AnswerClaim(
                claim=(
                    "For those who held a Tier 2 or Skilled Worker visa before "
                    "4 April 2024 and continually held Skilled Worker visas since, "
                    "the minimum salary is £31,300 and these lower requirements "
                    "apply only to applications made before 4 April 2030."
                ),
                evidence_ids=[ev_trans.evidence_id],
            ),
        ],
    )
    set_evidence_orchestrator_override(_FakeOrch())  # type: ignore[arg-type]
    set_llm_provider_override(llm)

    resp = run_agent(
        AgentRequest(
            question="What is the Skilled Worker threshold for pre-April 2024 holders?",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            retrieval={"external_mode": "off"},
        ),
        config=AgentConfig(
            llm_provider="fake",
            llm_model="fake",
            verification_enabled=True,
            verification_max_external_corroborations=0,
        ),
    )
    assert llm.calls == 1
    assert resp.status == AgentStatus.SUCCESS
    assert len(resp.claims) == 2
    assert "£31,300" in (resp.answer or "")
    assert "£41,700" in (resp.answer or "")
    assert (resp.verification or {}).get("suppressed_claims") == []
