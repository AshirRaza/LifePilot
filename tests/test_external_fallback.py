"""Selective external fallback / automatic hybrid retrieval tests (mocked)."""

from __future__ import annotations

from typing import Any

from lifepilot.agent.config import AgentConfig
from lifepilot.agent.contracts import AgentStatus
from lifepilot.agent.orchestration import EvidenceOrchestrator
from lifepilot.external.models import (
    ExternalEvidence,
    ProviderId,
    ProviderOutcome,
    RetrievalStatus,
    SourceType,
)


def test_auto_external_runs_when_internal_thin():
    external_calls = {"n": 0}
    orch = EvidenceOrchestrator(
        config=AgentConfig(external_fallback_on_insufficient_internal=False)
    )

    def _internal(**kwargs: Any):
        return [], "no_results", 1.0

    def _external(**kwargs: Any):
        external_calls["n"] += 1
        item = ExternalEvidence(
            provider=ProviderId.GOVUK_CONTENT,
            jurisdiction="GB",
            title="Skilled Worker",
            official_url="https://www.gov.uk/skilled-worker-visa",
            canonical_url="https://www.gov.uk/skilled-worker-visa",
            source_type=SourceType.GUIDANCE,
            passages=[
                "A Skilled Worker visa requires an approved sponsor and eligible job."
            ],
        )
        outcome = ProviderOutcome(
            provider=ProviderId.GOVUK_CONTENT,
            status=RetrievalStatus.OK,
            items=[item],
        )
        return [item], [outcome], 1.0

    orch._retrieve_internal = _internal  # type: ignore[method-assign]
    orch._retrieve_external = _external  # type: ignore[method-assign]
    result = orch.gather(
        question="Skilled Worker salary?",
        jurisdiction="GB",
        external_mode="auto",
        include_external=None,
    )
    assert external_calls["n"] == 1
    assert result.retrieval_decision in {
        "internal_plus_external",
        "external_verification_required",
    }
    assert any(e.source_type.value == "external_api" for e in result.evidence)
    assert all(
        e.verification_state.value == "unverified_external"
        for e in result.evidence
        if e.source_type.value == "external_api"
    )


def test_explicit_internal_only_skips_connectors_even_when_thin():
    calls = {"n": 0}
    orch = EvidenceOrchestrator(
        config=AgentConfig(external_fallback_on_insufficient_internal=True)
    )

    def _internal(**kwargs: Any):
        return [], "no_results", 1.0

    def _external(**kwargs: Any):
        calls["n"] += 1
        return [], [], 0.0

    orch._retrieve_internal = _internal  # type: ignore[method-assign]
    orch._retrieve_external = _external  # type: ignore[method-assign]
    result = orch.gather(
        question="Skilled Worker salary?",
        jurisdiction="GB",
        include_external=False,
    )
    assert calls["n"] == 0
    assert result.retrieval_decision == "internal_only"
    assert result.status == AgentStatus.INSUFFICIENT_EVIDENCE


def test_explicit_include_external_still_parallel_path():
    """include_external=True continues to request external even with fallback off."""
    calls = {"n": 0}
    orch = EvidenceOrchestrator(
        config=AgentConfig(external_fallback_on_insufficient_internal=False)
    )

    def _internal(**kwargs: Any):
        return [], "no_results", 1.0

    def _external(**kwargs: Any):
        calls["n"] += 1
        return [], [], 0.5

    orch._retrieve_internal = _internal  # type: ignore[method-assign]
    orch._retrieve_external = _external  # type: ignore[method-assign]
    orch.gather(
        question="Skilled Worker?",
        jurisdiction="GB",
        include_external=True,
    )
    assert calls["n"] == 1


def test_auto_thin_low_risk_triggers_external_without_legacy_env():
    """external_mode=auto already covers thin internal evidence (no env flag)."""
    calls = {"n": 0}
    orch = EvidenceOrchestrator(
        config=AgentConfig(external_fallback_on_insufficient_internal=False)
    )

    def _internal(**kwargs: Any):
        return [], "no_results", 1.0

    def _external(**kwargs: Any):
        calls["n"] += 1
        return [], [], 0.5

    orch._retrieve_internal = _internal  # type: ignore[method-assign]
    orch._retrieve_external = _external  # type: ignore[method-assign]
    result = orch.gather(
        question="Tell me about the Skilled Worker visa overview",
        jurisdiction="GB",
        external_mode="auto",
        include_external=None,
    )
    assert calls["n"] == 1
    assert result.retrieval_decision != "internal_only"
    assert any(
        "insufficient_internal" in r for r in (result.retrieval_decision_reasons or [])
    )
