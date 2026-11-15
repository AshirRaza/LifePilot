"""Phase 9D.2 unified automated legal verification — offline regressions."""

from __future__ import annotations

from lifepilot.agent.answer import AnswerClaim
from lifepilot.agent.config import AgentConfig, reset_agent_config_cache
from lifepilot.agent.contracts import AgentRequest, AgentStatus
from lifepilot.agent.evidence import evidence_from_internal_passage
from lifepilot.agent.graph import GRAPH_NODE_ORDER, reset_compiled_agent_graph
from lifepilot.agent.providers import (
    LLMUsage,
    StructuredLLMResult,
    reset_llm_provider_override,
    set_llm_provider_override,
)
from lifepilot.agent.runner import run_agent
from lifepilot.agent.verification import (
    ClaimVerificationStatus,
    verify_material_claims,
)
from lifepilot.agent.orchestration import (
    OrchestrationResult,
    reset_evidence_orchestrator_override,
    set_evidence_orchestrator_override,
)


def _ev(*, text: str, url: str, chunk_id: int = 1, jur: str = "IE"):
    return evidence_from_internal_passage(
        jurisdiction=jur,
        official_url=url,
        document_id=100,
        document_version_id=200,
        chunk_id=chunk_id,
        content_text=text,
        score=0.9,
    )


class _FakeOrch:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = 0

    def gather(self, **kwargs):
        self.calls += 1
        return OrchestrationResult(
            status=AgentStatus.SUCCESS,
            evidence=list(self.evidence),
            warnings=[],
            errors=[],
            missing_information=[],
            internal_ms=1.0,
            external_ms=0.0,
            normalize_ms=0.0,
            retrieval_decision="internal_only",
            external_provider_outcomes=[],
        )


class _FakeLLM:
    provider_id = "fake"
    model_id = "fake-model"

    def __init__(self, answer: str, claims: list[AnswerClaim]):
        self.answer = answer
        self.claims = claims
        self.calls = 0

    def complete(self, messages, *, max_tokens, temperature=0.0):
        raise AssertionError("complete() should not be used")

    def complete_structured(self, messages, *, schema, max_tokens, temperature=0.0):
        self.calls += 1
        from lifepilot.agent.answer import GroundedAnswer

        value = GroundedAnswer(
            answer=self.answer,
            claims=self.claims,
            missing_information=[],
            limitations=[],
        )
        return StructuredLLMResult(
            value=value,
            usage=LLMUsage(input_tokens=10, output_tokens=20, total_tokens=30),
            latency_ms=5.0,
            model=self.model_id,
            provider=self.provider_id,
        )


def test_graph_includes_legal_verification_node():
    assert "legal_verification" in GRAPH_NODE_ORDER
    assert GRAPH_NODE_ORDER.index("answer_extension_point") < GRAPH_NODE_ORDER.index(
        "legal_verification"
    )
    assert GRAPH_NODE_ORDER.index("legal_verification") < GRAPH_NODE_ORDER.index(
        "finalize"
    )


def test_supported_claim_retained():
    csep = _ev(
        text=(
            "Critical Skills Employment Permit criteria include occupation type "
            "and proposed remuneration. Remuneration must be at least €38,000 "
            "for some Critical Skills occupations."
        ),
        url="https://enterprise.gov.ie/critical-skills-employment-permit/",
    )
    claim = AnswerClaim(
        claim=(
            "Some Critical Skills Employment Permit occupations require "
            "remuneration of at least €38,000."
        ),
        evidence_ids=[csep.evidence_id],
    )
    result = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[csep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
        question="What remuneration thresholds apply to Critical Skills?",
        max_external_corroborations=0,
    )
    assert len(result.claims) == 1
    assert result.outcomes[0].status == ClaimVerificationStatus.SUPPORTED
    assert "€38,000" in (result.answer or "")


def test_unsupported_numeric_figure_suppressed():
    csep = _ev(
        text="Critical Skills Employment Permit depends on occupation and remuneration.",
        url="https://enterprise.gov.ie/critical-skills-employment-permit/",
    )
    claim = AnswerClaim(
        claim="The Critical Skills Employment Permit threshold is €99,999.",
        evidence_ids=[csep.evidence_id],
    )
    result = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[csep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
        question="Critical Skills salary?",
        max_external_corroborations=0,
    )
    assert result.claims == []
    assert result.outcomes[0].status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
    assert "€99,999" not in (result.answer or "")


def test_cross_pathway_salary_pathway_mismatch():
    gep = _ev(
        text=(
            "General Employment Permit remuneration criteria. "
            "Minimum annual remuneration of €68,911."
        ),
        url="https://enterprise.gov.ie/general-employment-permit/",
    )
    csep = _ev(
        text="Critical Skills Employment Permit eligibility depends on occupation type.",
        url="https://enterprise.gov.ie/critical-skills-employment-permit/",
        chunk_id=2,
    )
    claim = AnswerClaim(
        claim=(
            "The Critical Skills Employment Permit minimum annual remuneration "
            "is €68,911."
        ),
        evidence_ids=[gep.evidence_id, csep.evidence_id],
    )
    result = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[gep, csep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
        question="Critical Skills salary threshold?",
        max_external_corroborations=0,
    )
    assert result.claims == []
    assert result.outcomes[0].status == ClaimVerificationStatus.PATHWAY_MISMATCH
    assert "€68,911" not in (result.answer or "")


def test_related_context_numeric_without_comparison_suppressed():
    gep = _ev(
        text=(
            "General Employment Permit remuneration criteria. "
            "Minimum annual remuneration of €32,691."
        ),
        url="https://enterprise.gov.ie/general-employment-permit/",
    )
    claim = AnswerClaim(
        claim=(
            "The General Employment Permit has a minimum annual remuneration "
            "of €32,691."
        ),
        evidence_ids=[gep.evidence_id],
    )
    result = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[gep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
        question="Critical Skills salary threshold?",
        max_external_corroborations=0,
    )
    assert result.claims == []
    assert result.outcomes[0].status == ClaimVerificationStatus.PATHWAY_MISMATCH
    assert "€32,691" not in (result.answer or "")


def test_explicit_comparison_not_blocked():
    gep = _ev(
        text=(
            "General Employment Permit remuneration criteria include thresholds "
            "of €32,691. Critical Skills Employment Permit is a different permit type."
        ),
        url="https://enterprise.gov.ie/general-employment-permit/",
    )
    claim = AnswerClaim(
        claim=(
            "Unlike the Critical Skills Employment Permit, the General Employment "
            "Permit has a separate remuneration threshold of €32,691."
        ),
        evidence_ids=[gep.evidence_id],
    )
    result = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[gep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
        question="Compare Critical Skills and General Employment Permit pay?",
        max_external_corroborations=0,
    )
    assert len(result.claims) == 1
    assert result.outcomes[0].status == ClaimVerificationStatus.SUPPORTED


def test_future_effective_temporal_uncertainty():
    gb = _ev(
        text=(
            "Skilled Worker visa changes. From 1 April 2027 the general salary "
            "threshold will be £50,000. Do not apply the future figure early."
        ),
        url="https://www.gov.uk/skilled-worker-visa",
        jur="GB",
    )
    claim = AnswerClaim(
        claim="The current Skilled Worker general salary threshold is £50,000.",
        evidence_ids=[gb.evidence_id],
    )
    result = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[gb],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        question="What is the current Skilled Worker salary threshold?",
        max_external_corroborations=0,
    )
    assert result.outcomes[0].status == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY
    assert result.claims[0].uncertainty
    assert "unresolved" in (result.claims[0].uncertainty or "").lower() or "effective" in (
        result.claims[0].uncertainty or ""
    ).lower()


def test_historical_requirement_uncertainty():
    gb = _ev(
        text=(
            "Previously the Skilled Worker threshold was £26,200. That historical "
            "figure no longer applies under current guidance."
        ),
        url="https://www.gov.uk/skilled-worker-visa",
        jur="GB",
    )
    claim = AnswerClaim(
        claim="The Skilled Worker salary threshold is £26,200.",
        evidence_ids=[gb.evidence_id],
    )
    result = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[gb],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        question="Skilled Worker salary?",
        max_external_corroborations=0,
    )
    assert result.outcomes[0].status == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY


def test_mixed_supported_and_unsupported_claims():
    csep = _ev(
        text=(
            "Critical Skills Employment Permit. Some occupations require "
            "remuneration of at least €38,000."
        ),
        url="https://enterprise.gov.ie/critical-skills-employment-permit/",
    )
    good = AnswerClaim(
        claim=(
            "Some Critical Skills Employment Permit occupations require "
            "remuneration of at least €38,000."
        ),
        evidence_ids=[csep.evidence_id],
    )
    bad = AnswerClaim(
        claim="The Critical Skills Employment Permit fee is exactly €12,345.",
        evidence_ids=[csep.evidence_id],
    )
    result = verify_material_claims(
        answer=f"{good.claim} {bad.claim}",
        claims=[good, bad],
        evidence=[csep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
        question="Critical Skills requirements?",
        max_external_corroborations=0,
    )
    assert len(result.claims) == 1
    assert "€38,000" in (result.answer or "")
    assert "€12,345" not in (result.answer or "")
    assert any(o.status == ClaimVerificationStatus.SUPPORTED for o in result.outcomes)
    assert any(
        o.status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE for o in result.outcomes
    )


def test_verification_idempotent():
    csep = _ev(
        text=(
            "Critical Skills Employment Permit. Remuneration must be at least "
            "€38,000 for some Critical Skills occupations."
        ),
        url="https://enterprise.gov.ie/critical-skills-employment-permit/",
    )
    claim = AnswerClaim(
        claim=(
            "Some Critical Skills Employment Permit occupations require "
            "remuneration of at least €38,000."
        ),
        evidence_ids=[csep.evidence_id],
    )
    first = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[csep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
        question="Critical Skills salary?",
        max_external_corroborations=0,
    )
    second = verify_material_claims(
        answer=first.answer,
        claims=first.claims,
        evidence=[csep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
        question="Critical Skills salary?",
        max_external_corroborations=0,
    )
    assert [c.claim for c in first.claims] == [c.claim for c in second.claims]
    assert first.outcomes[0].status == second.outcomes[0].status


def test_no_unnecessary_external_calls_by_default():
    class Boom:
        def corroborate(self, **kwargs):
            raise AssertionError("external corroboration should not run")

    csep = _ev(
        text=(
            "Critical Skills Employment Permit. Remuneration must be at least "
            "€38,000 for some Critical Skills occupations."
        ),
        url="https://enterprise.gov.ie/critical-skills-employment-permit/",
    )
    claim = AnswerClaim(
        claim=(
            "Some Critical Skills Employment Permit occupations require "
            "remuneration of at least €38,000."
        ),
        evidence_ids=[csep.evidence_id],
    )
    result = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[csep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
        question="Critical Skills salary?",
        corroborator=Boom(),
        max_external_corroborations=0,
    )
    assert result.report.external_calls == 0


def test_provider_failure_conservatively_qualifies(monkeypatch):
    class FailCorr:
        def corroborate(self, **kwargs):
            raise RuntimeError("connector down")

    csep = _ev(
        text=(
            "Critical Skills Employment Permit. Remuneration must be at least "
            "€38,000 for some Critical Skills occupations."
        ),
        url="https://enterprise.gov.ie/critical-skills-employment-permit/",
    )
    claim = AnswerClaim(
        claim=(
            "Some Critical Skills Employment Permit occupations require "
            "remuneration of at least €38,000."
        ),
        evidence_ids=[csep.evidence_id],
    )
    result = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[csep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
        question="What is the Critical Skills salary threshold?",
        corroborator=FailCorr(),
        max_external_corroborations=1,
    )
    # Failure should not crash; claim may be kept with qualification or insufficient.
    assert result.report.external_calls == 1
    assert any("corroboration" in w for w in result.warnings) or result.claims


def test_run_agent_invokes_verification(monkeypatch):
    reset_agent_config_cache()
    reset_compiled_agent_graph()
    csep = _ev(
        text=(
            "Critical Skills Employment Permit. Remuneration must be at least "
            "€38,000 for some Critical Skills occupations."
        ),
        url="https://enterprise.gov.ie/critical-skills-employment-permit/",
    )
    bad_gep_mix = _ev(
        text="General Employment Permit minimum annual remuneration of €68,911.",
        url="https://enterprise.gov.ie/general-employment-permit/",
        chunk_id=9,
    )
    orch = _FakeOrch([csep, bad_gep_mix])
    llm = _FakeLLM(
        answer=(
            "The Critical Skills Employment Permit minimum is €68,911. "
            "Some occupations require €38,000."
        ),
        claims=[
            AnswerClaim(
                claim=(
                    "The Critical Skills Employment Permit minimum annual "
                    "remuneration is €68,911."
                ),
                evidence_ids=[csep.evidence_id, bad_gep_mix.evidence_id],
            ),
            AnswerClaim(
                claim=(
                    "Some Critical Skills Employment Permit occupations require "
                    "remuneration of at least €38,000."
                ),
                evidence_ids=[csep.evidence_id],
            ),
        ],
    )
    set_evidence_orchestrator_override(orch)
    set_llm_provider_override(llm)
    try:
        resp = run_agent(
            AgentRequest(
                question="What remuneration thresholds apply to Critical Skills?",
                jurisdiction="IE",
                pathway_id="ie_critical_skills_employment_permit",
                retrieval={"mode": "hybrid", "top_k": 4, "external_mode": "off"},
            ),
            config=AgentConfig(
                llm_provider="fake",
                llm_model="fake",
                verification_enabled=True,
                verification_max_external_corroborations=0,
            ),
        )
    finally:
        reset_evidence_orchestrator_override()
        reset_llm_provider_override()
        reset_compiled_agent_graph()
        reset_agent_config_cache()

    assert resp.verification is not None
    assert resp.timing.verification_ms is not None
    assert "Automated verification assesses evidence support" in " ".join(
        resp.warnings or []
    )
    assert llm.calls == 1
    assert "€68,911" not in (resp.answer or "")
    assert "€38,000" in (resp.answer or "")
    # Grounding and/or verification must strip the cross-pathway figure.
    outcomes = (resp.verification or {}).get("claim_outcomes") or []
    assert outcomes
    assert all(
        "€68,911" not in (o.get("claim") or "") or not o.get("retained")
        for o in outcomes
    )


def test_citation_integrity_after_verification():
    csep = _ev(
        text=(
            "Critical Skills Employment Permit. Remuneration must be at least "
            "€38,000 for some Critical Skills occupations."
        ),
        url="https://enterprise.gov.ie/critical-skills-employment-permit/",
    )
    claim = AnswerClaim(
        claim=(
            "Some Critical Skills Employment Permit occupations require "
            "remuneration of at least €38,000."
        ),
        evidence_ids=[csep.evidence_id],
    )
    result = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[csep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
        question="Critical Skills?",
        max_external_corroborations=0,
    )
    assert result.claims[0].evidence_ids == [csep.evidence_id]


def test_cross_jurisdiction_claim_insufficient():
    gb = _ev(
        text="Skilled Worker visa salary threshold guidance on GOV.UK.",
        url="https://www.gov.uk/skilled-worker-visa",
        jur="GB",
    )
    # Force a claim that cites GB evidence while verifying as IE — simulate mismatch.
    claim = AnswerClaim(
        claim="Ireland Critical Skills uses the UK Skilled Worker threshold of £38,700.",
        evidence_ids=[gb.evidence_id],
    )
    result = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[gb],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
        question="Ireland salary?",
        max_external_corroborations=0,
    )
    assert result.claims == []
    assert result.outcomes[0].status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
