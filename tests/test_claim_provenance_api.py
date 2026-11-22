"""Phase 10C.4 — Claim-level provenance, API transparency & corroboration integrity.

Synthetic examples only. No live DB. No extra LLM calls.
"""

from __future__ import annotations

import json

from lifepilot.agent.answer import AnswerClaim
from lifepilot.agent.claim_provenance import (
    ClaimEvidenceRef,
    build_claim_evidence_ref,
    build_claim_evidence_refs,
    derive_supporting_excerpt,
    project_claim_verification_for_api,
    project_evidence_item_for_api,
    same_underlying_document,
)
from lifepilot.agent.evidence import (
    EvidenceVerificationState,
    evidence_from_external_item,
    evidence_from_internal_passage,
)
from lifepilot.agent.verification import (
    ClaimVerificationStatus,
    search_distinct_corroboration,
    verify_material_claims,
)
from lifepilot.api.app import _to_answer_response
from lifepilot.api.schemas import AnswerResponse
from lifepilot.agent.contracts import AgentResponse, AgentStatus, TimingMetadata


def _internal(
    *,
    text: str,
    url: str = "https://www.gov.uk/skilled-worker-visa",
    chunk_id: int = 1,
    document_id: int = 100,
    document_version_id: int = 200,
    jur: str = "GB",
    heading_path: str | None = "Salary thresholds",
    char_start: int | None = 10,
    char_end: int | None = 200,
    is_truncated: bool = False,
    temporal_provenance: dict | None = None,
    content_sha256: str | None = None,
    legal_status: str | None = "in_force",
):
    return evidence_from_internal_passage(
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
        is_truncated=is_truncated,
        temporal_provenance=temporal_provenance,
        document_version_number=3,
        chunk_index=chunk_id,
        content_sha256=content_sha256
        or f"sha-{document_id}-{document_version_id}-{chunk_id}",
        authority_name="Home Office",
        legal_status=legal_status,
    )


# ---------------------------------------------------------------------------
# Provenance contract / excerpts
# ---------------------------------------------------------------------------

def test_claim_to_evidence_id_resolution_and_internal_identity():
    ev = _internal(
        text="The Skilled Worker general salary threshold is £41,700 per year."
    )
    claim = AnswerClaim(
        claim="The Skilled Worker general salary threshold is £41,700 per year.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        question="What is the Skilled Worker salary threshold?",
        max_external_corroborations=0,
    )
    assert res.outcomes[0].retained is True
    assert res.outcomes[0].evidence_ids == [ev.evidence_id]
    assert len(res.outcomes[0].evidence_refs) == 1
    ref = res.outcomes[0].evidence_refs[0]
    assert ref.evidence_id == ev.evidence_id
    assert ref.document_id == 100
    assert ref.document_version_id == 200
    assert ref.document_version_number == 3
    assert ref.chunk_id == 1
    assert ref.chunk_index == 1
    assert ref.heading_path == "Salary thresholds"
    assert ref.char_start == 10
    assert ref.char_end == 200
    assert ref.content_sha256 == "sha-100-200-1"
    assert ref.excerpt_authenticated is True
    assert "£41,700" in (ref.excerpt or "")


def test_external_evidence_missing_identifiers_not_fabricated():
    ext = evidence_from_external_item(
        jurisdiction="GB",
        official_url="https://www.gov.uk/guidance/synthetic-external",
        provider="govuk",
        passage_text="The synthetic application fee is £719.",
        source_type_label="guidance",
        legal_status="in_force",
    )
    assert ext.document_id is None
    assert ext.chunk_id is None
    ref = build_claim_evidence_ref(
        "The synthetic application fee is £719.",
        ext,
    )
    assert ref.document_id is None
    assert ref.chunk_id is None
    assert "external_evidence_document_id_unavailable" in ref.provenance_limitations
    assert ref.excerpt_authenticated is True


def test_exact_excerpt_authenticity_and_truncated_marking():
    ev = _internal(
        text="Sponsored workers must be paid the going rate for the occupation …[truncated]",
        is_truncated=True,
    )
    excerpt, auth, trunc, lims = derive_supporting_excerpt(
        "Sponsored workers must be paid the going rate for the occupation.",
        ev,
    )
    assert auth is True
    assert trunc is True
    assert excerpt and excerpt in ev.passage_text or excerpt.replace(
        " …[excerpt_bounded]", ""
    ) in ev.passage_text
    assert "source_passage_truncated_conditions_may_be_omitted" in lims


def test_temporal_and_cohort_provenance_on_refs():
    ev = _internal(
        text=(
            "Under transitional arrangements, applicants who continually held Tier 2 leave "
            "may meet a lower threshold of £29,000 per year."
        ),
        temporal_provenance={
            "effective_from": "2024-04-04",
            "effective_to": "2030-04-04",
            "cohort_key": "transitional_tier2",
            "is_transitional": True,
        },
    )
    ref = build_claim_evidence_ref(
        "Under transitional arrangements, applicants who continually held Tier 2 leave "
        "may meet a lower threshold of £29,000 per year.",
        ev,
    )
    assert ref.effective_from == "2024-04-04"
    assert ref.effective_to == "2030-04-04"
    assert ref.cohort_key == "transitional_tier2"
    assert ref.is_transitional is True
    assert ref.source_authority_category is not None


def test_authority_and_human_verification_not_promoted():
    ev = _internal(text="The Skilled Worker general salary threshold is £41,700 per year.")
    assert ev.verification_state == EvidenceVerificationState.UNVERIFIED_RETRIEVAL
    ref = build_claim_evidence_ref(
        "The Skilled Worker general salary threshold is £41,700 per year.",
        ev,
    )
    assert ref.human_review_state == "unreviewed_retrieval"
    assert "source_not_human_verified_for_claim" in ref.provenance_limitations
    assert ref.mechanical_match_only is True


def test_qualifier_inversion_diagnostics_on_outcome():
    ev = _internal(
        text="The going rate for this occupation is at most £38,700 per year."
    )
    claim = AnswerClaim(
        claim="The going rate for this occupation is at least £38,700 per year.",
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
    assert res.outcomes[0].retained is False
    assert res.outcomes[0].evidence_refs == []


def test_wrong_pathway_rejection_diagnostics():
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
        evidence=[ie],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].retained is False
    assert res.outcomes[0].status in {
        ClaimVerificationStatus.PATHWAY_MISMATCH,
        ClaimVerificationStatus.INSUFFICIENT_EVIDENCE,
        ClaimVerificationStatus.CONTRADICTED,
    }


# ---------------------------------------------------------------------------
# Q-10 corroboration integrity
# ---------------------------------------------------------------------------

def test_independent_corroboration_distinct_documents():
    a = _internal(
        text=(
            "Skilled Worker visa. The general salary threshold is £41,700 per year "
            "or the going rate, whichever is higher."
        ),
        url="https://www.gov.uk/skilled-worker-visa",
        document_id=1,
        document_version_id=11,
        chunk_id=1,
    )
    b = _internal(
        text=(
            "Skilled Worker visa guidance. The general salary threshold is £41,700 "
            "per year or the going rate for the occupation, whichever is higher."
        ),
        url="https://www.gov.uk/government/publications/skilled-worker-visa-caseworker-guidance",
        document_id=2,
        document_version_id=22,
        chunk_id=1,
    )
    claim = (
        "The Skilled Worker general salary threshold is £41,700 per year or "
        "the going rate, whichever is higher."
    )
    found = search_distinct_corroboration(
        claim=claim,
        cited=[a],
        all_evidence=[a, b],
        pathway_id="gb_skilled_worker",
    )
    assert found.item is not None
    assert found.item.evidence_id == b.evidence_id


def test_same_document_pseudo_corroboration_rejected():
    a = _internal(
        text="The general salary threshold is £41,700 per year.",
        url="https://www.gov.uk/skilled-worker-visa",
        document_id=42,
        document_version_id=420,
        chunk_id=1,
    )
    b = _internal(
        text="The general salary threshold is £41,700 per year for Skilled Worker.",
        url="https://www.gov.uk/skilled-worker-visa/your-job",
        document_id=42,
        document_version_id=420,
        chunk_id=2,
    )
    assert same_underlying_document(a, b) is True
    found = search_distinct_corroboration(
        claim="The Skilled Worker general salary threshold is £41,700 per year.",
        cited=[a],
        all_evidence=[a, b],
        pathway_id="gb_skilled_worker",
    )
    assert found.item is None
    assert any("same_underlying_document" in r for r in found.rejection_reasons)


def test_same_amount_different_cohort_not_corroboration():
    general = _internal(
        text="The general salary threshold is £33,400 per year.",
        url="https://www.gov.uk/skilled-worker-visa",
        document_id=1,
        document_version_id=1,
    )
    new_entrant = _internal(
        text="New entrants may use a lower threshold of £33,400 per year.",
        url="https://www.gov.uk/guidance/new-entrant-salary",
        document_id=2,
        document_version_id=2,
        chunk_id=2,
    )
    claim = (
        "Under the new entrant rules, applicants may use a lower threshold of "
        "£33,400 per year."
    )
    found = search_distinct_corroboration(
        claim=claim,
        cited=[new_entrant],
        all_evidence=[new_entrant, general],
        pathway_id="gb_skilled_worker",
    )
    # General page lacks new_entrant cohort — should not corroborate scoped claim.
    assert found.item is None or found.item.evidence_id != general.evidence_id
    if found.item is None:
        assert any(
            "cohort" in r or "qualifier" in r for r in found.rejection_reasons
        )


def test_same_amount_different_legal_period_not_corroboration():
    current = _internal(
        text=(
            "From 4 April 2024 the general salary threshold is £41,700 per year."
        ),
        url="https://www.gov.uk/skilled-worker-visa",
        document_id=1,
        document_version_id=1,
    )
    historical = _internal(
        text=(
            "Before 4 April 2024 the general salary threshold was £26,200 per year. "
            "A previous figure of £41,700 does not apply to that period."
        ),
        url="https://www.gov.uk/archive/skilled-worker-2023",
        document_id=9,
        document_version_id=9,
        chunk_id=3,
    )
    # Numeric won't match £41,700 on historical if only £26,200 — use same amount different dates:
    historical2 = _internal(
        text=(
            "Applications made before 4 April 2024 were assessed against a "
            "£41,700 figure under a different legal period."
        ),
        url="https://www.gov.uk/archive/skilled-worker-legacy",
        document_id=10,
        document_version_id=10,
        chunk_id=4,
    )
    claim = (
        "Applications made after 4 April 2024 are assessed under the general "
        "salary threshold of £41,700 per year."
    )
    found = search_distinct_corroboration(
        claim=claim,
        cited=[current],
        all_evidence=[current, historical2],
        pathway_id="gb_skilled_worker",
    )
    assert found.item is None or found.item.evidence_id != historical2.evidence_id


def test_corroborator_omitted_mandatory_conditions_rejected():
    cited = _internal(
        text=(
            "Applicants do not need sponsorship if they have unrestricted work authorization. "
            "The relevant fee example is £719."
        ),
        url="https://www.gov.uk/page-a",
        document_id=1,
        document_version_id=1,
    )
    weak = _internal(
        text="Applicants do not need sponsorship. The fee example is £719.",
        url="https://www.gov.uk/page-b",
        document_id=2,
        document_version_id=2,
        chunk_id=2,
    )
    claim = (
        "Applicants do not need sponsorship if they have unrestricted work "
        "authorization; a fee example is £719."
    )
    found = search_distinct_corroboration(
        claim=claim,
        cited=[cited],
        all_evidence=[cited, weak],
        pathway_id="gb_skilled_worker",
    )
    # Weak passage strips the condition relative to the claim → reject.
    assert found.item is None or found.item.evidence_id != weak.evidence_id


# ---------------------------------------------------------------------------
# API projection / compatibility
# ---------------------------------------------------------------------------

def test_rejected_claim_does_not_leak_into_api_metadata():
    ev = _internal(
        text="The going rate is at most £38,700 per year for this occupation."
    )
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
    agent = AgentResponse(
        request_id="req_test",
        status=AgentStatus.SUCCESS,
        evidence=[ev],
        answer=res.answer,
        claims=res.claims,
        verification=res.report.model_dump(mode="json"),
        timing=TimingMetadata(total_ms=1.0),
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    compact = _to_answer_response(agent, include_evidence=False)
    assert compact.claim_verification is not None
    assert all(v.get("retained") for v in compact.claim_verification)
    assert all("at least" not in (v.get("claim") or "").lower() for v in compact.claim_verification)
    assert all(c["claim"] != bad.claim for c in compact.claims)
    assert bad.claim not in (compact.answer or "")


def test_api_compact_vs_detailed_evidence_modes():
    ev = _internal(text="The Skilled Worker general salary threshold is £41,700 per year.")
    claim = AnswerClaim(
        claim="The Skilled Worker general salary threshold is £41,700 per year.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        question="What is the Skilled Worker salary threshold?",
        max_external_corroborations=0,
    )
    agent = AgentResponse(
        request_id="req_modes",
        status=AgentStatus.SUCCESS,
        evidence=[ev],
        answer=res.answer,
        claims=res.claims,
        verification=res.report.model_dump(mode="json"),
        timing=TimingMetadata(total_ms=1.0),
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    compact = _to_answer_response(agent, include_evidence=False)
    assert compact.evidence is None
    assert compact.evidence_detail == "none"
    assert compact.claim_verification is not None
    assert compact.claim_verification[0]["status"] in {"supported", "temporal_uncertainty"}
    assert compact.claim_verification[0]["evidence_refs"]
    assert compact.claim_verification[0]["evidence_refs"][0].get("excerpt") is None
    # Essential authority still present on refs without full passage.
    assert compact.claim_verification[0]["evidence_refs"][0].get("source_authority_category")
    assert compact.verification is not None

    detailed = _to_answer_response(agent, include_evidence=True)
    assert detailed.evidence is not None
    assert detailed.evidence_detail == "detailed"
    assert detailed.evidence[0]["document_version_id"] == 200
    assert detailed.evidence[0]["passage_text"]
    assert detailed.claim_verification[0]["evidence_refs"][0].get("excerpt")
    assert detailed.claim_verification[0]["evidence_refs"][0]["excerpt_authenticated"] is True


def test_api_response_size_bounds_and_compatibility_fields():
    ev = _internal(
        text=("The Skilled Worker general salary threshold is £41,700 per year. " * 40)
    )
    claim = AnswerClaim(
        claim="The Skilled Worker general salary threshold is £41,700 per year.",
        evidence_ids=[ev.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[ev],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        question="What is the Skilled Worker salary threshold?",
        max_external_corroborations=0,
    )
    assert res.outcomes[0].retained is True
    agent = AgentResponse(
        request_id="req_size",
        status=AgentStatus.SUCCESS,
        evidence=[ev],
        answer=res.answer,
        claims=res.claims,
        verification=res.report.model_dump(mode="json"),
        timing=TimingMetadata(total_ms=1.0),
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    detailed = _to_answer_response(agent, include_evidence=True)
    assert detailed.claim_verification
    payload = detailed.model_dump(mode="json")
    serialized = json.dumps(payload)
    assert len(serialized) < 200_000
    excerpt = detailed.claim_verification[0]["evidence_refs"][0]["excerpt"]
    assert excerpt is None or len(excerpt) <= 520
    # Legacy fields still present.
    assert "claims" in payload and "verification" in payload and "evidence_count" in payload
    AnswerResponse.model_validate(payload)


def test_zero_additional_llm_calls_for_provenance(monkeypatch):
    calls = {"n": 0}

    def _boom(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("LLM must not be called")

    monkeypatch.setattr(
        "lifepilot.agent.providers.get_llm_provider", _boom, raising=False
    )
    ev = _internal(text="The Skilled Worker general salary threshold is £41,700 per year.")
    refs = build_claim_evidence_refs(
        "The Skilled Worker general salary threshold is £41,700 per year.",
        [ev],
    )
    assert len(refs) == 1
    assert calls["n"] == 0


def test_numeric_currency_diagnostics_still_surface():
    ev = _internal(text="The general salary threshold is £41,700 per year.")
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
    assert any("unit" in r or "mismatch" in r for r in res.outcomes[0].reasons)


def test_project_helpers_omit_rejected_and_bound_passage():
    from lifepilot.agent.verification import ClaimVerificationOutcome

    ev = _internal(text="hello " * 400)
    compact = project_evidence_item_for_api(ev, detailed=False)
    assert "passage_text" not in compact
    detailed = project_evidence_item_for_api(ev, detailed=True)
    assert detailed["passage_text"].endswith("…[truncated]") or len(
        detailed["passage_text"]
    ) <= 1200

    oc_retained = ClaimVerificationOutcome(
        claim="ok",
        evidence_ids=["a"],
        status=ClaimVerificationStatus.SUPPORTED,
        retained=True,
        evidence_refs=[
            ClaimEvidenceRef(
                evidence_id="a",
                excerpt="secret-ish excerpt",
                excerpt_authenticated=True,
            )
        ],
    )
    views = project_claim_verification_for_api(
        [oc_retained], include_excerpts=False, retained_only=True
    )
    assert views[0]["evidence_refs"][0]["excerpt"] is None
