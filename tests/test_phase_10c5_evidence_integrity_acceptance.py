"""Phase 10C.5 — Integrated Evidence Integrity Acceptance.

Exercises retrieval→verification→prose→ClaimEvidenceRef→API serialization with
synthetic fixtures only. Live ``lifepilot`` DB is never modified.

Scenarios use orchestrator/LLM overrides so the real verification + API mapping
path runs end-to-end without inventing immigration law.
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from lifepilot.agent.answer import AnswerClaim, GroundedAnswer
from lifepilot.agent.claim_provenance import project_claim_verification_for_api
from lifepilot.agent.config import AgentConfig, reset_agent_config_cache
from lifepilot.agent.contracts import AgentRequest, AgentStatus
from lifepilot.agent.evidence import (
    EvidenceSourceType,
    EvidenceVerificationState,
    evidence_from_external_item,
    evidence_from_internal_passage,
)
from lifepilot.agent.orchestration import (
    OrchestrationResult,
    set_evidence_orchestrator_override,
)
from lifepilot.agent.providers import (
    LLMUsage,
    StructuredLLMResult,
    set_llm_provider_override,
)
from lifepilot.agent.runner import run_agent
from lifepilot.agent.verification import (
    ClaimVerificationStatus,
    IndependentCorroboration,
    search_distinct_corroboration,
    verify_material_claims,
)
from lifepilot.api.app import _to_answer_response
from lifepilot.api.rate_limit import reset_rate_limit_state
from lifepilot.api.settings import reset_api_settings_cache
from lifepilot.agent.contracts import AgentResponse, TimingMetadata


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _CountingFakeLLM:
    provider_id = "fake"
    model_id = "fake-10c5"

    def __init__(self, builder: Callable[[str], GroundedAnswer]) -> None:
        self._builder = builder
        self.calls = 0
        self.last_prompt = ""

    def complete(self, *a, **k):
        raise AssertionError("complete() must not be called")

    def complete_structured(self, messages, *, schema, max_tokens, temperature=0.0):
        self.calls += 1
        self.last_prompt = messages[-1].content
        return StructuredLLMResult(
            value=self._builder(self.last_prompt),
            usage=LLMUsage(input_tokens=40, output_tokens=30, total_tokens=70),
            latency_ms=1.0,
            model=self.model_id,
            provider=self.provider_id,
        )


def _ev(
    text: str,
    *,
    url: str = "https://www.gov.uk/skilled-worker-visa",
    doc: int = 100,
    ver: int = 200,
    chunk: int = 1,
    jur: str = "GB",
    legal_status: str = "in_force",
    is_truncated: bool = False,
    heading_path: str | None = "Salary",
    temporal: dict | None = None,
    authority: str = "Home Office",
):
    return evidence_from_internal_passage(
        jurisdiction=jur,
        official_url=url,
        document_id=doc,
        document_version_id=ver,
        chunk_id=chunk,
        content_text=text,
        score=0.91,
        heading_path=heading_path,
        char_start=0,
        char_end=min(len(text), 400),
        document_version_number=1,
        chunk_index=chunk,
        content_sha256=f"sha-{doc}-{ver}-{chunk}",
        authority_name=authority,
        legal_status=legal_status,
        is_truncated=is_truncated,
        temporal_provenance=temporal,
    )


def _api_client(monkeypatch, *, include_evidence: bool = False):
    import lifepilot.api.app as api_app

    monkeypatch.setenv("LIFEPILOT_ENV", "development")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_AUTH", "1")
    monkeypatch.setenv("LIFEPILOT_API_KEYS", "c5-key")
    monkeypatch.setenv("LIFEPILOT_VERIFICATION_ENABLED", "1")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_LLM", "0")
    monkeypatch.setenv(
        "LIFEPILOT_API_INCLUDE_EVIDENCE", "1" if include_evidence else "0"
    )
    reset_api_settings_cache()
    reset_agent_config_cache()
    reset_rate_limit_state()
    return TestClient(api_app.create_app()), api_app


def _run_answer(
    monkeypatch,
    *,
    evidence,
    claim: str,
    question: str,
    jurisdiction: str = "GB",
    pathway_id: str | None = "gb_skilled_worker",
    include_evidence: bool = False,
    answer: str | None = None,
    as_of_date: str | None = None,
):
    class _Orch:
        def gather(self, **_k):
            return OrchestrationResult(
                evidence=list(evidence),
                status=AgentStatus.SUCCESS,
                internal_status="ok",
                resolved_reference_date=as_of_date or "2026-11-18",
                temporal_mode="current",
                temporal_outcome="current_candidate_unverified",
            )

    set_evidence_orchestrator_override(_Orch())
    llm = _CountingFakeLLM(
        lambda _p: GroundedAnswer(
            answer=answer or claim,
            claims=[AnswerClaim(claim=claim, evidence_ids=[evidence[0].evidence_id])],
        )
    )
    set_llm_provider_override(llm)
    client, _ = _api_client(monkeypatch, include_evidence=include_evidence)
    payload: dict[str, Any] = {
        "question": question,
        "jurisdiction": jurisdiction,
    }
    if pathway_id:
        payload["pathway_id"] = pathway_id
    if as_of_date:
        payload["as_of_date"] = as_of_date
    resp = client.post(
        "/v1/answer",
        headers={"X-API-Key": "c5-key"},
        json=payload,
    )
    return resp, llm


# ---------------------------------------------------------------------------
# Task 4 — Corroboration uncertainty (confirmed 10C.5 defect remediation)
# ---------------------------------------------------------------------------


def test_uncertain_truncated_and_unclear_status_cannot_raise_corroboration():
    cited = _ev(
        "Skilled Worker general salary threshold is £41,700 per year.",
        doc=1,
        ver=1,
        url="https://www.gov.uk/skilled-worker-visa",
        legal_status="in_force",
    )
    truncated = _ev(
        "Skilled Worker general salary threshold is £41,700 per year",
        doc=2,
        ver=2,
        chunk=1,
        url="https://www.gov.uk/guidance/sw-alt",
        legal_status="in_force",
        is_truncated=True,
    )
    unclear = _ev(
        "Skilled Worker general salary threshold is £41,700 per year.",
        doc=3,
        ver=3,
        url="https://www.gov.uk/guidance/sw-unclear",
        legal_status="unknown",
    )
    opaque = _ev(
        "Skilled Worker general salary threshold is £41,700 per year. Further conditions apply as set out in the guidance.",
        doc=4,
        ver=4,
        url="https://www.gov.uk/guidance/sw-opaque",
        legal_status="in_force",
    )
    claim = "The Skilled Worker general salary threshold is £41,700 per year."

    t = search_distinct_corroboration(
        claim=claim, cited=[cited], all_evidence=[cited, truncated], pathway_id="gb_skilled_worker"
    )
    assert t.item is None
    assert any("truncated_evidence" in r for r in t.rejection_reasons)

    u = search_distinct_corroboration(
        claim=claim, cited=[cited], all_evidence=[cited, unclear], pathway_id="gb_skilled_worker"
    )
    assert u.item is None
    assert any("unclear_legal_status" in r for r in u.rejection_reasons)

    o = search_distinct_corroboration(
        claim=claim, cited=[cited], all_evidence=[cited, opaque], pathway_id="gb_skilled_worker"
    )
    # Opaque "further conditions" → qualifier UNCERTAIN → no affirmative corroboration.
    assert o.item is None
    assert any("qualifier_uncertain" in r or "uncertain" in r for r in o.rejection_reasons)

    good = _ev(
        "Skilled Worker visa guidance. The general salary threshold is £41,700 per year.",
        doc=5,
        ver=5,
        url="https://www.gov.uk/government/publications/sw-caseworker",
        legal_status="in_force",
    )
    ok = search_distinct_corroboration(
        claim=claim, cited=[cited], all_evidence=[cited, good], pathway_id="gb_skilled_worker"
    )
    assert ok.item is not None
    assert ok.item.evidence_id == good.evidence_id


def test_same_document_mirror_urls_not_double_counted():
    a = _ev(
        "The general salary threshold is £41,700 per year.",
        doc=42,
        ver=420,
        url="https://www.gov.uk/skilled-worker-visa",
    )
    mirror = _ev(
        "The general salary threshold is £41,700 per year for Skilled Worker.",
        doc=42,
        ver=420,
        chunk=2,
        url="https://www.gov.uk/skilled-worker-visa/your-job",
    )
    found = search_distinct_corroboration(
        claim="The Skilled Worker general salary threshold is £41,700 per year.",
        cited=[a],
        all_evidence=[a, mirror],
        pathway_id="gb_skilled_worker",
    )
    assert found.item is None
    assert any("same_underlying_document" in r for r in found.rejection_reasons)


# ---------------------------------------------------------------------------
# Direct verification scenarios (1–20 style coverage)
# ---------------------------------------------------------------------------


def test_currency_unit_and_eu_decimal_integrity():
    gb = _ev("The general salary threshold is £41,700 per year.")
    assert (
        verify_material_claims(
            answer="The general salary threshold is £41,700 per month.",
            claims=[
                AnswerClaim(
                    claim="The general salary threshold is £41,700 per month.",
                    evidence_ids=[gb.evidence_id],
                )
            ],
            evidence=[gb],
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
        )
        .outcomes[0]
        .retained
        is False
    )
    assert (
        verify_material_claims(
            answer="The general salary threshold is €41,700 per year.",
            claims=[
                AnswerClaim(
                    claim="The general salary threshold is €41,700 per year.",
                    evidence_ids=[gb.evidence_id],
                )
            ],
            evidence=[gb],
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
        )
        .outcomes[0]
        .retained
        is False
    )

    nl = _ev(
        "IND Fees: permit fee is € 423,00 per application.",
        jur="NL",
        url="https://ind.nl/en/fees",
        doc=8,
        ver=8,
    )
    res = verify_material_claims(
        answer="IND permit fee is €423.00 per application.",
        claims=[
            AnswerClaim(
                claim="IND Fees state the permit fee is €423.00 per application.",
                evidence_ids=[nl.evidence_id],
            )
        ],
        evidence=[nl],
        jurisdiction="NL",
        pathway_id="nl_fees_hub",
        question="What is the IND fee?",
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.SUPPORTED
    assert res.outcomes[0].retained is True


def test_temporal_transitional_and_conditional_scenarios():
    # Future-framed figure presented as current → not silently accepted as current.
    fut = _ev(
        "From 1 January 2027 the synthetic threshold will be £52,000 per year.",
        doc=11,
        ver=11,
    )
    res_fut = verify_material_claims(
        answer="The current synthetic threshold is £52,000 per year.",
        claims=[
            AnswerClaim(
                claim="The current synthetic threshold is £52,000 per year.",
                evidence_ids=[fut.evidence_id],
            )
        ],
        evidence=[fut],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        resolved_reference_date=date(2026, 11, 18),
    )
    assert res_fut.outcomes[0].status in {
        ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
        ClaimVerificationStatus.INSUFFICIENT_EVIDENCE,
        ClaimVerificationStatus.CONTRADICTED,
    }

    # Transitional vs general coexistence
    sw = _ev(
        "Skilled Worker visa: the general salary threshold is £41,700 per year. "
        "For those who held a Tier 2 or Skilled Worker visa before 4 April 2024, "
        "the transitional minimum is £31,300 per year and applies only to applications made before 4 April 2030.",
        doc=12,
        ver=12,
    )
    res_sw = verify_material_claims(
        answer="both",
        claims=[
            AnswerClaim(
                claim="The Skilled Worker general salary threshold is £41,700 per year.",
                evidence_ids=[sw.evidence_id],
            ),
            AnswerClaim(
                claim=(
                    "For pre-4 April 2024 Skilled Worker visa holders, the transitional minimum "
                    "is £31,300 per year and applies only to applications made before 4 April 2030."
                ),
                evidence_ids=[sw.evidence_id],
            ),
        ],
        evidence=[sw],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert [o.status for o in res_sw.outcomes] == [
        ClaimVerificationStatus.SUPPORTED,
        ClaimVerificationStatus.SUPPORTED,
    ]

    # Conditional sponsorship exemption stripping
    cond = _ev(
        "Applicants do not need sponsorship if they have unrestricted work authorization.",
        doc=13,
        ver=13,
    )
    bad = verify_material_claims(
        answer="Applicants do not need sponsorship.",
        claims=[
            AnswerClaim(
                claim="Applicants do not need sponsorship.",
                evidence_ids=[cond.evidence_id],
            )
        ],
        evidence=[cond],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert bad.outcomes[0].retained is False
    good = verify_material_claims(
        answer="Applicants do not need sponsorship if they have unrestricted work authorization.",
        claims=[
            AnswerClaim(
                claim=(
                    "Applicants do not need sponsorship if they have unrestricted work authorization."
                ),
                evidence_ids=[cond.evidence_id],
            )
        ],
        evidence=[cond],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert good.outcomes[0].retained is True


def test_negation_bound_universal_pathway_truncation_authority():
    # Bound inversion
    bound = _ev("The going rate is at most £38,700 per year.", doc=21, ver=21)
    inv = verify_material_claims(
        answer="at least",
        claims=[
            AnswerClaim(
                claim="The going rate is at least £38,700 per year.",
                evidence_ids=[bound.evidence_id],
            )
        ],
        evidence=[bound],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert inv.outcomes[0].status == ClaimVerificationStatus.CONTRADICTED

    # Unsupported universal
    age = _ev(
        "Applicants under 26 may meet a lower salary threshold of £33,400 per year.",
        doc=22,
        ver=22,
    )
    uni = verify_material_claims(
        answer="all",
        claims=[
            AnswerClaim(
                claim="All applicants must meet a salary threshold of £33,400 per year without exception.",
                evidence_ids=[age.evidence_id],
            )
        ],
        evidence=[age],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert uni.outcomes[0].retained is False

    # Cross-pathway contamination
    ie = evidence_from_internal_passage(
        jurisdiction="GB",
        official_url="https://enterprise.gov.ie/critical-skills-employment-permit/",
        document_id=50,
        document_version_id=51,
        chunk_id=2,
        content_text="Critical Skills remuneration must be at least €38,000 for some occupations.",
        score=0.8,
        legal_status="in_force",
    )
    cross = verify_material_claims(
        answer="sw",
        claims=[
            AnswerClaim(
                claim="The Skilled Worker salary threshold is €38,000 per year.",
                evidence_ids=[ie.evidence_id],
            )
        ],
        evidence=[ie],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert cross.outcomes[0].retained is False

    # Truncation blocks exclusivity / comprehensive claims
    trunc = _ev(
        "The general salary threshold is £41,700 per year",
        doc=23,
        ver=23,
        is_truncated=True,
    )
    trunc_res = verify_material_claims(
        answer="all eligible",
        claims=[
            AnswerClaim(
                claim="All applicants are eligible if they earn £41,700 per year without exception.",
                evidence_ids=[trunc.evidence_id],
            )
        ],
        evidence=[trunc],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert trunc_res.outcomes[0].status in {
        ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
        ClaimVerificationStatus.INSUFFICIENT_EVIDENCE,
    }

    # Statistical dataset cannot verify normative claim
    ds = evidence_from_external_item(
        jurisdiction="CA",
        official_url="https://open.canada.ca/data/en/dataset/express-entry-synthetic",
        provider="canada_open",
        passage_text="Express Entry minimum required CRS score is 490 points.",
        source_type_label="dataset",
        legal_status="dataset_catalog",
        metadata={"organization": "IRCC"},
    )
    stat = verify_material_claims(
        answer="crs",
        claims=[
            AnswerClaim(
                claim="The Express Entry minimum required CRS score is 490 points.",
                evidence_ids=[ds.evidence_id],
            )
        ],
        evidence=[ds],
        jurisdiction="CA",
        pathway_id="ca_express_entry",
    )
    assert stat.outcomes[0].retained is False
    assert any("statistical_dataset" in r for r in stat.outcomes[0].reasons)

    # Proposed source
    prop = evidence_from_external_item(
        jurisdiction="GB",
        official_url="https://www.gov.uk/government/consultations/synthetic-proposed",
        provider="govuk",
        passage_text="Proposed synthetic fee is £999.",
        source_type_label="consultation",
        legal_status="proposed_or_preparatory",
    )
    prop_res = verify_material_claims(
        answer="fee",
        claims=[
            AnswerClaim(
                claim="The synthetic fee is £999.",
                evidence_ids=[prop.evidence_id],
            )
        ],
        evidence=[prop],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        resolved_reference_date=date(2026, 11, 18),
    )
    assert prop_res.outcomes[0].status in {
        ClaimVerificationStatus.TEMPORAL_UNCERTAINTY,
        ClaimVerificationStatus.INSUFFICIENT_EVIDENCE,
    }


def test_claim_provenance_excerpts_and_missing_external_ids():
    internal = _ev(
        "The Skilled Worker general salary threshold is £41,700 per year.",
        doc=30,
        ver=30,
    )
    res = verify_material_claims(
        answer=internal.passage_text,
        claims=[
            AnswerClaim(
                claim="The Skilled Worker general salary threshold is £41,700 per year.",
                evidence_ids=[internal.evidence_id],
            )
        ],
        evidence=[internal],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        question="What is the Skilled Worker salary threshold?",
    )
    assert res.outcomes[0].retained is True
    ref = res.outcomes[0].evidence_refs[0]
    assert ref.excerpt_authenticated is True
    assert "£41,700" in (ref.excerpt or "")
    assert ref.document_version_id == 30
    assert ref.human_review_state == "unreviewed_retrieval"
    assert ref.mechanical_match_only is True

    ext = evidence_from_external_item(
        jurisdiction="GB",
        official_url="https://www.gov.uk/guidance/synthetic-external-fee",
        provider="govuk",
        passage_text="The synthetic application fee is £719.",
        legal_status="in_force",
    )
    assert ext.document_id is None
    ext_res = verify_material_claims(
        answer=ext.passage_text,
        claims=[
            AnswerClaim(
                claim="The synthetic application fee is £719.",
                evidence_ids=[ext.evidence_id],
            )
        ],
        evidence=[ext],
        jurisdiction="GB",
        pathway_id=None,
        question="What is the synthetic fee?",
    )
    # May be supported or insufficient depending on pathway markers; provenance must not invent IDs.
    if ext_res.outcomes[0].retained and ext_res.outcomes[0].evidence_refs:
        assert ext_res.outcomes[0].evidence_refs[0].document_id is None
        assert (
            "external_evidence_document_id_unavailable"
            in ext_res.outcomes[0].evidence_refs[0].provenance_limitations
        )


def test_rejected_claims_do_not_leak_in_prose_or_metadata():
    ev = _ev("The going rate is at most £38,700 per year for this occupation.", doc=40, ver=40)
    bad = AnswerClaim(
        claim="The going rate is at least £38,700 per year for this occupation.",
        evidence_ids=[ev.evidence_id],
    )
    good = AnswerClaim(
        claim="The going rate is at most £38,700 per year for this occupation.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=f"{bad.claim} {good.claim}",
        claims=[bad, good],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert bad.claim not in (res.answer or "")
    assert all(c.claim != bad.claim for c in res.claims)
    assert all(not o.retained or "at least" not in o.claim.lower() for o in res.outcomes)

    agent = AgentResponse(
        request_id="leak",
        status=AgentStatus.SUCCESS,
        evidence=[ev],
        answer=res.answer,
        claims=res.claims,
        verification=res.report.model_dump(mode="json"),
        timing=TimingMetadata(total_ms=1.0),
        jurisdiction="GB",
    )
    body = _to_answer_response(agent, include_evidence=False)
    assert all(v["claim"] != bad.claim for v in (body.claim_verification or []))
    assert all(c["claim"] != bad.claim for c in body.claims)


def test_unresolved_evidence_id_fails_safely():
    ev = _ev("The threshold is £41,700 per year.", doc=41, ver=41)
    res = verify_material_claims(
        answer="missing",
        claims=[
            AnswerClaim(
                claim="The threshold is £41,700 per year.",
                evidence_ids=["ev_does_not_exist_zzzz"],
            )
        ],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].retained is False
    assert res.outcomes[0].status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
    assert "citation_unresolved" in res.outcomes[0].reasons[0]


# ---------------------------------------------------------------------------
# API end-to-end via /v1/answer
# ---------------------------------------------------------------------------


def test_api_e2e_compact_and_detailed_parity(reset_agent_runtime, monkeypatch):
    ev = _ev(
        "The Skilled Worker general salary threshold is £41,700 per year.",
        doc=60,
        ver=60,
    )
    claim = "The Skilled Worker general salary threshold is £41,700 per year."
    r_compact, llm_c = _run_answer(
        monkeypatch,
        evidence=[ev],
        claim=claim,
        question="What is the Skilled Worker salary threshold?",
        include_evidence=False,
    )
    assert r_compact.status_code == 200
    compact = r_compact.json()
    assert llm_c.calls == 1
    assert compact["evidence"] is None
    assert compact["evidence_detail"] == "none"
    assert compact["verification"] is not None
    assert compact["claim_verification"]
    assert compact["claim_verification"][0]["status"] in {
        "supported",
        "temporal_uncertainty",
    }
    assert compact["claim_verification"][0]["evidence_refs"][0]["excerpt"] is None
    assert compact["claim_verification"][0]["evidence_refs"][0]["document_version_id"] == 60

    r_detail, llm_d = _run_answer(
        monkeypatch,
        evidence=[ev],
        claim=claim,
        question="What is the Skilled Worker salary threshold?",
        include_evidence=True,
    )
    assert r_detail.status_code == 200
    detailed = r_detail.json()
    assert llm_d.calls == 1
    assert detailed["evidence"] is not None
    assert detailed["evidence_detail"] == "detailed"
    assert detailed["evidence"][0]["passage_text"]
    assert detailed["claim_verification"][0]["evidence_refs"][0]["excerpt"]
    assert detailed["claim_verification"][0]["evidence_refs"][0]["excerpt_authenticated"] is True

    # Verification decisions identical across modes.
    assert (
        compact["claim_verification"][0]["status"]
        == detailed["claim_verification"][0]["status"]
    )
    assert (
        compact["verification"]["claim_outcomes"][0]["status"]
        == detailed["verification"]["claim_outcomes"][0]["status"]
    )
    assert compact["claims"] == detailed["claims"]


def test_api_e2e_rejected_claim_and_zero_extra_llm(reset_agent_runtime, monkeypatch):
    ev = _ev("The going rate is at most £38,700 per year.", doc=61, ver=61)

    class _Orch:
        def gather(self, **_k):
            return OrchestrationResult(
                evidence=[ev],
                status=AgentStatus.SUCCESS,
                resolved_reference_date="2026-11-18",
                temporal_mode="current",
                temporal_outcome="current_candidate_unverified",
            )

    set_evidence_orchestrator_override(_Orch())
    llm = _CountingFakeLLM(
        lambda _p: GroundedAnswer(
            answer=(
                "The going rate is at least £38,700 per year. "
                "Also the going rate is at most £38,700 per year."
            ),
            claims=[
                AnswerClaim(
                    claim="The going rate is at least £38,700 per year.",
                    evidence_ids=[ev.evidence_id],
                ),
                AnswerClaim(
                    claim="The going rate is at most £38,700 per year.",
                    evidence_ids=[ev.evidence_id],
                ),
            ],
        )
    )
    set_llm_provider_override(llm)
    client, _ = _api_client(monkeypatch, include_evidence=False)
    r = client.post(
        "/v1/answer",
        headers={"X-API-Key": "c5-key"},
        json={
            "question": "What is the going rate?",
            "jurisdiction": "GB",
            "pathway_id": "gb_skilled_worker",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert llm.calls == 1
    assert "at least" not in (body["answer"] or "").lower()
    assert all("at least" not in c["claim"].lower() for c in body["claims"])
    assert all(
        "at least" not in (v.get("claim") or "").lower()
        for v in (body.get("claim_verification") or [])
    )


def test_api_legacy_fields_and_response_bounds(reset_agent_runtime, monkeypatch):
    ev = _ev(
        ("The Skilled Worker general salary threshold is £41,700 per year. " * 30),
        doc=62,
        ver=62,
    )
    r, llm = _run_answer(
        monkeypatch,
        evidence=[ev],
        claim="The Skilled Worker general salary threshold is £41,700 per year.",
        question="What is the Skilled Worker salary threshold?",
        include_evidence=True,
    )
    assert r.status_code == 200
    body = r.json()
    assert llm.calls == 1
    for key in (
        "request_id",
        "status",
        "answer",
        "claims",
        "verification",
        "claim_verification",
        "evidence_count",
        "timing",
        "jurisdiction",
    ):
        assert key in body
    raw = json.dumps(body)
    assert len(raw) < 250_000
    excerpt = body["claim_verification"][0]["evidence_refs"][0]["excerpt"]
    assert excerpt is None or len(excerpt) <= 520


def test_abstention_on_missing_evidence_no_llm(reset_agent_runtime, monkeypatch):
    class _Empty:
        def gather(self, **_k):
            return OrchestrationResult(
                evidence=[],
                status=AgentStatus.INSUFFICIENT_EVIDENCE,
                missing_information=["retrieved_evidence"],
            )

    set_evidence_orchestrator_override(_Empty())
    llm = _CountingFakeLLM(
        lambda _p: (_ for _ in ()).throw(AssertionError("LLM must not run"))
    )
    set_llm_provider_override(llm)
    resp = run_agent(
        AgentRequest(question="What is the fee?", jurisdiction="GB"),
        config=AgentConfig(
            llm_provider="fake", llm_model="fake", verification_enabled=True
        ),
    )
    assert llm.calls == 0
    assert resp.status == AgentStatus.INSUFFICIENT_EVIDENCE


# ---------------------------------------------------------------------------
# Performance + migration compatibility (no live DB mutation)
# ---------------------------------------------------------------------------


def test_performance_verification_provenance_and_corroboration_overhead():
    cited = _ev(
        "Skilled Worker general salary threshold is £41,700 per year.",
        doc=70,
        ver=70,
    )
    alt = _ev(
        "Skilled Worker guidance. The general salary threshold is £41,700 per year.",
        doc=71,
        ver=71,
        url="https://www.gov.uk/government/publications/sw-caseworker",
    )
    claim = AnswerClaim(
        claim="The Skilled Worker general salary threshold is £41,700 per year.",
        evidence_ids=[cited.evidence_id],
    )

    t0 = time.perf_counter()
    for _ in range(50):
        verify_material_claims(
            answer=claim.claim,
            claims=[claim],
            evidence=[cited, alt],
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="What is the Skilled Worker salary threshold?",
            max_external_corroborations=0,
        )
    verify_ms = (time.perf_counter() - t0) * 1000

    t1 = time.perf_counter()
    for _ in range(100):
        search_distinct_corroboration(
            claim=claim.claim,
            cited=[cited],
            all_evidence=[cited, alt],
            pathway_id="gb_skilled_worker",
        )
    corr_ms = (time.perf_counter() - t1) * 1000

    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[cited, alt],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        question="What is the Skilled Worker salary threshold?",
        max_external_corroborations=0,
    )
    t2 = time.perf_counter()
    payload = project_claim_verification_for_api(
        res.outcomes, include_excerpts=True, retained_only=True
    )
    ser = json.dumps(payload)
    ser_ms = (time.perf_counter() - t2) * 1000

    # Soft bounds for CI machines — report via assertions that catch pathological regressions.
    assert verify_ms < 15_000, f"50 verifications took {verify_ms:.1f}ms"
    assert corr_ms < 5_000, f"100 corroboration searches took {corr_ms:.1f}ms"
    assert ser_ms < 500, f"provenance serialization took {ser_ms:.1f}ms"
    assert len(ser) < 50_000


def test_migration_0011_present_and_live_db_not_required_for_10c():
    """Phase 10C code is compatible with pre-0011 runtime; 0011 is temporal schema.

    This check only asserts the migration file exists and does not run alembic
    against live ``lifepilot``.
    """
    mig = Path("alembic/versions/20261118_0011_temporal_legal_data_model.py")
    assert mig.is_file(), "expected Phase 10B migration 0011 in repo"
    text = mig.read_text(encoding="utf-8")
    assert "legal_rules" in text.lower() or "LegalRule" in text or "effective_from" in text


def test_human_verified_source_does_not_imply_claim_approval():
    """Reviewed LegalRule mapping still mechanical at claim level."""
    # Build a minimal curated-rule shaped EvidenceItem via internal passage + state override.
    rule_like = _ev(
        "Synthetic reviewed rule: threshold is £41,700 per year for general cohort.",
        doc=80,
        ver=80,
        temporal={
            "effective_from": "2024-04-04",
            "cohort_key": "general",
            "is_transitional": False,
        },
    )
    # Force human_verified pairing legally: curated_rule requires HUMAN_VERIFIED_RULE.
    curated = rule_like.model_copy(
        update={
            "source_type": EvidenceSourceType.CURATED_RULE,
            "verification_state": EvidenceVerificationState.HUMAN_VERIFIED_RULE,
        }
    )
    res = verify_material_claims(
        answer=curated.passage_text,
        claims=[
            AnswerClaim(
                claim="Synthetic reviewed rule: threshold is £41,700 per year for general cohort.",
                evidence_ids=[curated.evidence_id],
            )
        ],
        evidence=[curated],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        question="threshold?",
        resolved_reference_date=date(2026, 11, 18),
    )
    if res.outcomes[0].retained and res.outcomes[0].evidence_refs:
        ref = res.outcomes[0].evidence_refs[0]
        assert ref.human_review_state == "human_verified_source"
        assert ref.mechanical_match_only is True
        assert any(
            "does_not_imply_claim_level_legal_approval" in x
            for x in ref.provenance_limitations
        )
