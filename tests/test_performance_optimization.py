"""Phase 9D.4 performance optimization regressions (deterministic)."""

from __future__ import annotations

from lifepilot.agent.answer import (
    build_user_prompt,
    dedupe_overlapping_evidence,
    pack_evidence_context,
    strip_retrieval_boilerplate,
)
from lifepilot.agent.config import AgentConfig, reset_agent_config_cache
from lifepilot.agent.evidence import evidence_from_internal_passage
from lifepilot.agent.orchestration import EvidenceOrchestrator
from lifepilot.agent.retrieval_policy import (
    ExternalMode,
    decide_retrieval,
    detect_high_risk_categories,
)
from lifepilot.external.http import ExternalHttpClient


def _ev(text: str, *, url: str, chunk_id: int = 1):
    return evidence_from_internal_passage(
        jurisdiction="GB",
        official_url=url,
        document_id=10,
        document_version_id=20,
        chunk_id=chunk_id,
        content_text=text,
        score=0.9,
    )


def test_regulatory_change_does_not_match_bare_current():
    cats = detect_high_risk_categories("What is the current Certificate of Sponsorship?")
    assert "regulatory_change" not in cats


def test_current_salary_still_high_risk():
    cats = detect_high_risk_categories("What is the current Skilled Worker minimum salary?")
    assert "salary_threshold" in cats


def test_explicit_new_rules_still_flagged():
    cats = detect_high_risk_categories("What are the new rules for Skilled Worker visas?")
    assert "regulatory_change" in cats


def test_salary_still_requires_external_under_auto():
    decision = decide_retrieval(
        question="What is the current Skilled Worker minimum salary?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        external_mode=ExternalMode.AUTO,
        substantive_internal_count=3,
        internal_status="ok",
        freshness=None,
        connectors_available=True,
    )
    assert decision.run_external is True


def test_fee_still_requires_external_under_auto():
    decision = decide_retrieval(
        question="How much is the UK visa application fee?",
        jurisdiction="GB",
        pathway_id=None,
        external_mode=ExternalMode.AUTO,
        substantive_internal_count=3,
        internal_status="ok",
        freshness=None,
        connectors_available=True,
    )
    assert decision.run_external is True


def test_low_risk_fresh_internal_stays_internal_only():
    from datetime import datetime, timedelta, timezone

    from lifepilot.agent.retrieval_policy import assess_internal_freshness

    now = datetime.now(timezone.utc)
    freshness = assess_internal_freshness(
        urls=["https://www.gov.uk/guidance/overview"],
        version_retrieved_at={10: now - timedelta(hours=1)},
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
    assert decision.run_external is False
    assert decision.decision.value == "internal_only"


def test_boilerplate_stripped_but_substance_kept():
    text = (
        "Cookie preferences Accept all cookies. "
        "The Skilled Worker general salary threshold is £41,700. "
        "Skip to main content."
    )
    cleaned = strip_retrieval_boilerplate(text)
    assert "£41,700" in cleaned
    assert "Accept all cookies" not in cleaned


def test_near_duplicate_evidence_deduped_same_url():
    url = "https://www.gov.uk/skilled-worker-visa"
    a = _ev(
        "The Skilled Worker general salary threshold is £41,700 per year or the going rate.",
        url=url,
        chunk_id=1,
    )
    b = _ev(
        "The Skilled Worker general salary threshold is £41,700 per year or the going rate. Navigation menu.",
        url=url,
        chunk_id=2,
    )
    c = _ev(
        "Applicants usually also pay the immigration health surcharge.",
        url="https://www.gov.uk/skilled-worker-visa/fees",
        chunk_id=3,
    )
    out = dedupe_overlapping_evidence([a, b, c])
    assert len(out) == 2
    assert out[0].evidence_id == a.evidence_id
    assert out[1].evidence_id == c.evidence_id


def test_pack_and_prompt_omit_empty_limitations_and_internal_ids():
    item = _ev(
        "Certificate of Sponsorship must be issued by an approved sponsor.",
        url="https://www.gov.uk/skilled-worker-visa",
    )
    packed = pack_evidence_context(
        [item],
        max_items=8,
        max_chars_per_passage=1200,
        max_total_chars=14000,
    )
    assert packed
    assert "document_id" not in packed[0]
    assert packed[0]["limitations"] is None
    prompt = build_user_prompt(
        question="What is a CoS?",
        jurisdiction="GB",
        pathway_id=None,
        user_circumstances=None,
        evidence_payload=packed,
    )
    assert "Shared applicability note:" in prompt
    assert prompt.count("Retrieved material is not validated pathway evidence") == 1
    assert "document_id" not in prompt
    assert packed[0]["evidence_id"] in prompt


def test_agent_router_attaches_http_cache():
    reset_agent_config_cache()
    cfg = AgentConfig(external_http_cache_ttl_seconds=600.0, external_http_disk_cache=False)
    orch = EvidenceOrchestrator(config=cfg)
    try:
        router = orch._get_router()
        assert isinstance(router.http, ExternalHttpClient)
        assert router.http.cache is not None
        assert router.cache is router.http.cache
        assert router.http.cache.default_ttl_seconds == 600.0
    finally:
        orch.close()
        reset_agent_config_cache()


def test_off_and_on_external_modes_unchanged():
    off = decide_retrieval(
        question="What is the current Skilled Worker minimum salary?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        external_mode=ExternalMode.OFF,
        substantive_internal_count=0,
        internal_status="no_results",
        connectors_available=True,
    )
    assert off.run_external is False
    on = decide_retrieval(
        question="What does a Certificate of Sponsorship mean?",
        jurisdiction="GB",
        pathway_id=None,
        external_mode=ExternalMode.ON,
        substantive_internal_count=5,
        internal_status="ok",
        connectors_available=True,
    )
    assert on.run_external is True
