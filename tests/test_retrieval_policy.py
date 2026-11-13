"""Deterministic tests for Phase 9C.4 intelligent hybrid retrieval policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from lifepilot.agent.config import AgentConfig
from lifepilot.agent.contracts import AgentRequest, AgentStatus, RetrievalOptions
from lifepilot.agent.evidence import (
    EvidenceItem,
    EvidenceSourceType,
    EvidenceVerificationState,
)
from lifepilot.agent.orchestration import EvidenceOrchestrator
from lifepilot.agent.retrieval_policy import (
    ExternalMode,
    RetrievalDecision,
    assess_internal_freshness,
    decide_retrieval,
    detect_high_risk_categories,
    reconcile_effective_dates,
    resolve_external_mode,
)
from lifepilot.external.models import (
    ExternalEvidence,
    ProviderId,
    ProviderOutcome,
    RetrievalStatus,
    SourceType,
)
from lifepilot.rag.retrieval import RetrievedPassage


def _passage(
    *,
    text: str,
    url: str = "https://www.gov.uk/guidance/general-info",
    version_id: int = 1,
    score: float = 0.9,
) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=1,
        document_id=1,
        document_version_id=version_id,
        jurisdiction_code="GB",
        official_url=url,
        content_text=text,
        score=score,
        heading_path=None,
        source_id=1,
        applicability_warning=None,
    )


def _mock_orch(*, internal=None, external=None, ages=None) -> EvidenceOrchestrator:
    orch = EvidenceOrchestrator(
        config=AgentConfig(external_fallback_on_insufficient_internal=False)
    )
    if internal is not None:
        orch._retrieve_internal = internal  # type: ignore[method-assign]
    if external is not None:
        orch._retrieve_external = external  # type: ignore[method-assign]
    orch._lookup_version_retrieved_at = (  # type: ignore[method-assign]
        lambda version_ids: ages or {}
    )
    return orch


def test_resolve_external_mode_legacy_bridge():
    assert resolve_external_mode(external_mode="auto", include_external=None) == ExternalMode.AUTO
    assert resolve_external_mode(external_mode="auto", include_external=False) == ExternalMode.OFF
    assert resolve_external_mode(external_mode="off", include_external=True) == ExternalMode.ON


def test_default_retrieval_options_are_auto():
    opts = RetrievalOptions()
    assert opts.external_mode == "auto"
    assert opts.include_external is None
    req = AgentRequest(question="What is a sponsor?", jurisdiction="GB")
    assert req.retrieval.external_mode == "auto"


def test_high_risk_detection_salary_and_fee():
    assert "salary_threshold" in detect_high_risk_categories(
        "What is the current Skilled Worker salary threshold?"
    )
    assert "application_fee" in detect_high_risk_categories(
        "How much is the visa application fee?"
    )


def test_general_question_fresh_evidence_internal_only():
    now = datetime.now(timezone.utc)
    ages = {10: now - timedelta(hours=2)}
    freshness = assess_internal_freshness(
        urls=["https://www.gov.uk/guidance/overview"],
        version_retrieved_at=ages,
        version_ids=[10],
        question_categories=[],
        now=now,
    )
    decision = decide_retrieval(
        question="What does a Certificate of Sponsorship mean?",
        jurisdiction="GB",
        pathway_id=None,
        external_mode=ExternalMode.AUTO,
        substantive_internal_count=3,
        internal_status="ok",
        freshness=freshness,
        connectors_available=True,
    )
    assert decision.decision == RetrievalDecision.INTERNAL_ONLY
    assert decision.run_external is False


def test_current_salary_triggers_external_verification():
    decision = decide_retrieval(
        question="What is the current Skilled Worker minimum salary?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        external_mode=ExternalMode.AUTO,
        substantive_internal_count=3,
        internal_status="ok",
        freshness=assess_internal_freshness(
            urls=["https://www.gov.uk/skilled-worker-visa"],
            version_retrieved_at={},
            version_ids=[],
            question_categories=["salary_threshold"],
        ),
        connectors_available=True,
    )
    assert decision.run_external is True
    assert decision.decision in {
        RetrievalDecision.INTERNAL_PLUS_EXTERNAL,
        RetrievalDecision.EXTERNAL_VERIFICATION_REQUIRED,
    }


def test_application_fee_triggers_external():
    decision = decide_retrieval(
        question="How much is the UK visa application fee?",
        jurisdiction="GB",
        pathway_id=None,
        external_mode=ExternalMode.AUTO,
        substantive_internal_count=2,
        internal_status="ok",
        freshness=None,
        connectors_available=True,
    )
    assert decision.run_external is True


def test_stale_internal_triggers_external():
    now = datetime.now(timezone.utc)
    freshness = assess_internal_freshness(
        urls=["https://www.gov.uk/skilled-worker-visa"],
        version_retrieved_at={1: now - timedelta(hours=100)},
        version_ids=[1],
        question_categories=["salary_threshold"],
        now=now,
    )
    assert freshness.stale is True
    decision = decide_retrieval(
        question="Skilled Worker salary threshold?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        external_mode=ExternalMode.AUTO,
        substantive_internal_count=2,
        internal_status="ok",
        freshness=freshness,
        connectors_available=True,
    )
    assert decision.run_external is True


def test_thin_internal_triggers_external():
    decision = decide_retrieval(
        question="Explain Skilled Worker documents",
        jurisdiction="GB",
        pathway_id=None,
        external_mode=ExternalMode.AUTO,
        substantive_internal_count=0,
        internal_status="no_results",
        freshness=None,
        connectors_available=True,
    )
    assert decision.run_external is True
    assert "insufficient_internal_evidence" in decision.reasons


def test_review_queue_uncertainty_triggers_external():
    decision = decide_retrieval(
        question="Confirm Skilled Worker going rate",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        external_mode=ExternalMode.AUTO,
        substantive_internal_count=3,
        internal_status="ok",
        freshness=assess_internal_freshness(
            urls=["https://www.gov.uk/skilled-worker-visa"],
            version_retrieved_at={1: datetime.now(timezone.utc)},
            version_ids=[1],
            question_categories=["salary_threshold"],
        ),
        connectors_available=True,
    )
    assert decision.run_external is True
    assert any(r.startswith("review_queue:") for r in decision.reasons)


def test_low_risk_fresh_no_unnecessary_connectors():
    calls = {"n": 0}
    now = datetime.now(timezone.utc)
    passages = [
        _passage(
            text="A Certificate of Sponsorship is assigned by a licensed sponsor for a specific role.",
            url="https://www.gov.uk/guidance/sponsor-a-skilled-worker",
            version_id=5,
        ),
        _passage(
            text="Sponsors must hold a valid sponsor licence before assigning certificates.",
            url="https://www.gov.uk/guidance/sponsor-a-skilled-worker",
            version_id=5,
        ),
    ]

    def _internal(**kwargs: Any):
        return passages, "ok", 1.0

    def _external(**kwargs: Any):
        calls["n"] += 1
        return [], [], 0.0

    orch = _mock_orch(
        internal=_internal,
        external=_external,
        ages={5: now - timedelta(hours=1)},
    )
    result = orch.gather(
        question="What is a Certificate of Sponsorship?",
        jurisdiction="GB",
        external_mode="auto",
        include_external=None,
    )
    assert calls["n"] == 0
    assert result.retrieval_decision == "internal_only"
    assert result.evidence


def test_external_provider_failure_safe_answer_path():
    passages = [
        _passage(
            text="Skilled Worker applicants need a job offer from an approved sponsor.",
            url="https://www.gov.uk/skilled-worker-visa",
        ),
        _passage(
            text="The role must meet skill and salary criteria published on GOV.UK.",
            url="https://www.gov.uk/skilled-worker-visa",
            version_id=2,
        ),
    ]

    def _internal(**kwargs: Any):
        return passages, "ok", 1.0

    def _external(**kwargs: Any):
        raise TimeoutError("connector timeout")

    orch = _mock_orch(
        internal=_internal,
        external=_external,
        ages={1: datetime.now(timezone.utc) - timedelta(days=10)},
    )
    result = orch.gather(
        question="What is the current Skilled Worker salary threshold?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        external_mode="auto",
    )
    assert result.status == AgentStatus.SUCCESS
    assert any("external_verification_attempted_but_failed" in w for w in result.warnings)
    assert result.evidence


def test_unsupported_jurisdiction_safe_degradation():
    decision = decide_retrieval(
        question="What is the current Employment Pass salary?",
        jurisdiction="SG",
        pathway_id="sg_employment_pass",
        external_mode=ExternalMode.AUTO,
        substantive_internal_count=2,
        internal_status="ok",
        freshness=assess_internal_freshness(
            urls=["https://www.mom.gov.sg/passes-and-permits/employment-pass"],
            version_retrieved_at={},
            version_ids=[],
            question_categories=["salary_threshold"],
        ),
        connectors_available=False,
    )
    assert decision.decision == RetrievalDecision.EXTERNAL_VERIFICATION_REQUIRED
    assert decision.run_external is False
    assert decision.require_verification is True


def test_numeric_conflict_adds_missing_information():
    internal_text = "The general salary threshold is £38,700 a year for sponsored roles."
    external_text = "The general salary threshold is £41,700 a year for sponsored roles."
    url = "https://www.gov.uk/skilled-worker-visa"

    def _internal(**kwargs: Any):
        return [
            _passage(text=internal_text, url=url),
            _passage(
                text="Sponsored jobs must meet going rates where applicable under the route.",
                url=url,
                version_id=2,
            ),
        ], "ok", 1.0

    def _external(**kwargs: Any):
        item = ExternalEvidence(
            provider=ProviderId.GOVUK_CONTENT,
            jurisdiction="GB",
            title="SW",
            official_url=url,
            canonical_url=url,
            source_type=SourceType.GUIDANCE,
            passages=[external_text],
        )
        return [item], [
            ProviderOutcome(
                provider=ProviderId.GOVUK_CONTENT,
                status=RetrievalStatus.OK,
                items=[item],
            )
        ], 1.0

    orch = _mock_orch(internal=_internal, external=_external, ages={})
    result = orch.gather(
        question="What is the current Skilled Worker salary threshold?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        external_mode="auto",
    )
    assert any("numeric_conflict" in w for w in result.warnings)
    assert "resolved_current_numerical_requirement" in result.missing_information
    assert any(
        e.verification_state == EvidenceVerificationState.UNVERIFIED_EXTERNAL
        for e in result.evidence
    )


def test_future_effective_not_treated_as_current():
    ev = EvidenceItem(
        evidence_id="ev_future_effective_01",
        source_type=EvidenceSourceType.INTERNAL_RAG,
        verification_state=EvidenceVerificationState.UNVERIFIED_RETRIEVAL,
        jurisdiction="GB",
        source_url="https://www.gov.uk/example",
        passage_text="The new threshold is effective from 1 January 2099 for this route.",
        document_id=1,
        document_version_id=1,
        chunk_id=1,
        retrieval_score=0.5,
    )
    notes = reconcile_effective_dates([ev])
    assert notes.future_effective_mentions
    assert any("future_effective" in w for w in notes.warnings)


def test_historical_question_prefers_internal():
    decision = decide_retrieval(
        question="What was the Skilled Worker salary threshold in 2020?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        external_mode=ExternalMode.AUTO,
        substantive_internal_count=3,
        internal_status="ok",
        freshness=None,
        connectors_available=True,
    )
    assert decision.run_external is False
    assert decision.decision == RetrievalDecision.INTERNAL_ONLY


def test_explicit_internal_only_mode_no_connector_calls():
    calls = {"n": 0}

    def _internal(**kwargs: Any):
        return [
            _passage(text="Overview of the Skilled Worker route for sponsored employment."),
            _passage(text="Applicants need a Certificate of Sponsorship from a licensed sponsor."),
        ], "ok", 1.0

    def _external(**kwargs: Any):
        calls["n"] += 1
        return [], [], 0.0

    orch = _mock_orch(internal=_internal, external=_external)
    result = orch.gather(
        question="What is the current Skilled Worker salary threshold?",
        jurisdiction="GB",
        include_external=False,
    )
    assert calls["n"] == 0
    assert result.retrieval_decision == "internal_only"


def test_agent_request_include_external_false_forces_off():
    req = AgentRequest(
        question="salary?",
        jurisdiction="GB",
        retrieval={"mode": "hybrid", "top_k": 8, "include_external": False},
    )
    assert req.retrieval.external_mode == "off"
