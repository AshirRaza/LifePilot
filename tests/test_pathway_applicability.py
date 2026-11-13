"""Pathway-aware grounding regressions (F07-style cross-pathway attribution)."""

from __future__ import annotations

from lifepilot.agent.answer import AnswerClaim, GroundedAnswer
from lifepilot.agent.evidence import evidence_from_internal_passage
from lifepilot.agent.grounding import validate_and_ground_answer
from lifepilot.agent.pathway_applicability import (
    PathwayApplicability,
    classify_pathway_applicability,
)


def _ev(*, text: str, url: str, chunk_id: int = 1):
    return evidence_from_internal_passage(
        jurisdiction="IE",
        official_url=url,
        document_id=100,
        document_version_id=200,
        chunk_id=chunk_id,
        content_text=text,
        score=0.9,
    )


def test_cross_pathway_salary_attribution_dropped():
    gep = _ev(
        text=(
            "General Employment Permit remuneration criteria. "
            "Minimum annual remuneration of €68,911 for eligible employment."
        ),
        url="https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/permit-types/general-employment-permit/",
    )
    raw = GroundedAnswer(
        answer="The Critical Skills Employment Permit minimum is €68,911.",
        claims=[
            AnswerClaim(
                claim=(
                    "The Critical Skills Employment Permit minimum annual "
                    "remuneration is €68,911."
                ),
                evidence_ids=[gep.evidence_id],
            )
        ],
    )
    result = validate_and_ground_answer(
        raw,
        evidence=[gep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
    )
    assert result.answer is not None
    assert result.answer.claims == []
    assert any("pathway_applicability" in w for w in result.warnings)
    assert "€68,911" not in (result.answer.answer or "")


def test_correctly_attributed_csep_salary_kept():
    csep = _ev(
        text=(
            "Critical Skills Employment Permit criteria include occupation type "
            "and proposed remuneration. Remuneration must be at least €38,000 "
            "for some Critical Skills occupations."
        ),
        url="https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/permit-types/critical-skills-employment-permit/",
        chunk_id=2,
    )
    raw = GroundedAnswer(
        answer="Some Critical Skills occupations require at least €38,000.",
        claims=[
            AnswerClaim(
                claim=(
                    "Some Critical Skills Employment Permit occupations require "
                    "remuneration of at least €38,000."
                ),
                evidence_ids=[csep.evidence_id],
            )
        ],
    )
    result = validate_and_ground_answer(
        raw,
        evidence=[csep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
    )
    assert len(result.answer.claims) == 1
    assert "€38,000" in result.answer.answer


def test_explicit_cross_pathway_comparison_kept():
    gep = _ev(
        text=(
            "General Employment Permit remuneration criteria include thresholds "
            "of €32,691. Critical Skills Employment Permit is a different permit type."
        ),
        url="https://enterprise.gov.ie/general-employment-permit/",
    )
    raw = GroundedAnswer(
        answer="GEP thresholds differ from Critical Skills.",
        claims=[
            AnswerClaim(
                claim=(
                    "Unlike the Critical Skills Employment Permit, the General "
                    "Employment Permit has a separate remuneration threshold of €32,691."
                ),
                evidence_ids=[gep.evidence_id],
            )
        ],
    )
    result = validate_and_ground_answer(
        raw,
        evidence=[gep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
    )
    assert len(result.answer.claims) == 1
    assert "€32,691" in result.answer.answer


def test_related_context_other_pathway_rule_kept_with_uncertainty():
    gep = _ev(
        text=(
            "General Employment Permit remuneration criteria. "
            "Minimum annual remuneration of €32,691."
        ),
        url="https://enterprise.gov.ie/general-employment-permit/",
    )
    raw = GroundedAnswer(
        answer="GEP has its own threshold.",
        claims=[
            AnswerClaim(
                claim=(
                    "The General Employment Permit has a minimum annual remuneration "
                    "of €32,691."
                ),
                evidence_ids=[gep.evidence_id],
            )
        ],
    )
    classified = classify_pathway_applicability(
        claim=raw.claims[0].claim,
        cited=[gep],
        pathway_id="ie_critical_skills_employment_permit",
    )
    assert classified.classification in {
        PathwayApplicability.RELATED_CONTEXT,
        PathwayApplicability.EXPLICIT_COMPARISON,
    }
    result = validate_and_ground_answer(
        raw,
        evidence=[gep],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
    )
    assert len(result.answer.claims) == 1
    assert result.answer.claims[0].uncertainty


def test_ambiguous_numeric_without_pathway_markers_dropped():
    vague = _ev(
        text="Remuneration must be at least €68,911 for eligible employment.",
        url="https://enterprise.gov.ie/en/employment-permits/overview/",
    )
    raw = GroundedAnswer(
        answer="Minimum is €68,911.",
        claims=[
            AnswerClaim(
                claim="The minimum annual remuneration is €68,911.",
                evidence_ids=[vague.evidence_id],
            )
        ],
    )
    result = validate_and_ground_answer(
        raw,
        evidence=[vague],
        jurisdiction="IE",
        pathway_id="ie_critical_skills_employment_permit",
    )
    assert result.answer.claims == []


def test_no_pathway_hint_skips_pathway_drop():
    gep = _ev(
        text="General Employment Permit minimum annual remuneration of €68,911.",
        url="https://enterprise.gov.ie/general-employment-permit/",
    )
    raw = GroundedAnswer(
        answer="Minimum €68,911.",
        claims=[
            AnswerClaim(
                claim="Minimum annual remuneration of €68,911 applies.",
                evidence_ids=[gep.evidence_id],
            )
        ],
    )
    result = validate_and_ground_answer(
        raw,
        evidence=[gep],
        jurisdiction="IE",
        pathway_id=None,
    )
    assert len(result.answer.claims) == 1
