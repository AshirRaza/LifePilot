"""Phase 6J regression: pathway-aware evidence matching and readiness."""

from __future__ import annotations

from pathlib import Path

import pytest

from lifepilot.coverage.corpus_reliability.assessment import (
    assess_document_for_pathway,
    match_requirement,
    reassess_suspicious_rows,
    validate_existing_association,
)
from lifepilot.coverage.corpus_reliability.human_review import (
    apply_reviews_to_readiness,
    record_decision,
)
from lifepilot.coverage.corpus_reliability.pathway_applicability import (
    document_applies_to_pathway,
)
from lifepilot.coverage.corpus_reliability.readiness import (
    audit_readiness,
    pathway_is_provisionally_ready,
    summarize_readiness,
)
from lifepilot.coverage.corpus_reliability.consistency import audit_evidence_consistency
from unittest.mock import MagicMock


def _blocks(*paragraphs: str) -> list[dict]:
    out: list[dict] = []
    for i, p in enumerate(paragraphs):
        if i == 0:
            out.append({"type": "heading", "text": p[:80]})
        else:
            out.append({"type": "paragraph", "text": p})
    return out


def test_f1_rejects_entrepreneur_parole_page():
    url = "https://www.uscis.gov/working-in-the-united-states/international-entrepreneur-rule"
    result = document_applies_to_pathway(pathway_id="us_f1", source_url=url)
    assert result.applies is False
    assert result.strength == "excluded"

    # Keyword match alone is not enough
    matched, _, passage = match_requirement(
        "Evidence Checklist for Form I-941 How to Apply as an Entrepreneur",
        "required_documents",
    )
    assert matched
    assessment = assess_document_for_pathway(
        _blocks("Eligibility", "Evidence Checklist " + "required documents " * 20),
        "us_f1",
        "required_documents",
        1549,
        1824,
        url,
    )
    assert assessment is None


def test_h1b_o1_e2_reject_unrelated_categories():
    entrepreneur = "https://www.uscis.gov/working-in-the-united-states/international-entrepreneur-rule"
    permanent = "https://www.uscis.gov/working-in-the-united-states/permanent-workers"
    for pid in ("us_h1b", "us_o1", "us_e2"):
        assert document_applies_to_pathway(pathway_id=pid, source_url=entrepreneur).applies is False
        assert document_applies_to_pathway(pathway_id=pid, source_url=permanent).applies is False


def test_au_485_rejects_student_500():
    url = "https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500"
    result = document_applies_to_pathway(pathway_id="au_temporary_graduate_485", source_url=url)
    assert result.applies is False
    ok, reason = validate_existing_association(
        pathway_id="au_temporary_graduate_485",
        requirement="eligibility",
        source_url=url,
        passage="Eligible applicants for student visa subclass 500 " * 5,
        classification="PARTIAL",
    )
    assert ok is False
    assert "exclusion" in reason or "Student" in reason or "student" in reason.lower() or "hard_exclusion" in reason


def test_de_blue_card_rejects_short_stay_hub():
    url = "https://www.auswaertiges-amt.de/en/visa-service/215870-215870"
    result = document_applies_to_pathway(pathway_id="de_eu_blue_card", source_url=url)
    assert result.applies is False


def test_generic_guidance_does_not_satisfy_pathway_requirement():
    url = "https://www.uscis.gov/working-in-the-united-states"
    result = document_applies_to_pathway(pathway_id="us_f1", source_url=url)
    assert result.applies is False
    assessment = assess_document_for_pathway(
        _blocks(
            "Eligibility",
            "Aliens who are eligible for employment authorization. " * 10,
        ),
        "us_f1",
        "eligibility",
        1547,
        1822,
        url,
    )
    assert assessment is None


def test_pathway_aware_accepts_matching_url_and_section():
    url = "https://www.uscis.gov/working-in-the-united-states/h-1b-specialty-occupations"
    body = (
        "H-1B specialty occupation eligibility criteria require a bachelor degree "
        "in a related field and a job offer from a US employer. " * 3
    )
    assessment = assess_document_for_pathway(
        [
            {"type": "heading", "text": "Eligibility criteria"},
            {"type": "paragraph", "text": body},
        ],
        "us_h1b",
        "eligibility",
        100,
        200,
        url,
        title="H-1B Specialty Occupations",
    )
    assert assessment is not None
    assert assessment.new_classification == "PARTIAL"
    assert assessment.method in {
        "pathway_aware_machine",
        "pathway_requirement_aware_machine",
    }
    assert assessment.new_classification != "COMPLETE"


def test_reassess_rejects_phase_6i_false_positives():
    rows = [
        {
            "pathway_id": "us_f1",
            "requirement": "required_documents",
            "entity_type": "true_pathway",
            "classification": "PARTIAL",
            "document_id": "1549",
            "version_id": "1824",
            "source_url": "https://www.uscis.gov/working-in-the-united-states/international-entrepreneur-rule",
            "passage": "Evidence Checklist for entrepreneur parole " * 5,
            "method": "machine_assessed",
            "country": "US",
        }
    ]
    prior = [
        {
            "pathway_id": "us_f1",
            "requirement": "required_documents",
            "new_classification": "PARTIAL",
        }
    ]
    updated, changes = reassess_suspicious_rows(rows, prior_machine_changes=prior)
    assert updated[0]["classification"] == "UNEVALUATED"
    assert len(changes) == 1
    assert changes[0]["action"] == "reject_invalid_association"


def test_rejected_evidence_cannot_contribute_to_readiness(tmp_path: Path):
    evidence_rows = [
        {
            "pathway_id": "gb_skilled_worker",
            "requirement": req,
            "classification": "COMPLETE",
            "document_id": "100",
            "version_id": "200",
            "source_url": "https://www.gov.uk/skilled-worker-visa",
            "entity_type": "true_pathway",
            "passage": "Detailed skilled worker eligibility and process guidance. " * 5,
            "country": "GB",
            "explanation": "n/a ok" if req == "financial_or_sponsorship" else "ok",
            "not_applicable_rationale": "",
        }
        for req in [
            "eligibility",
            "required_documents",
            "application_process",
            "financial_or_sponsorship",
            "official_provenance",
        ]
    ]
    assert pathway_is_provisionally_ready(evidence_rows, "gb_skilled_worker")

    decision = record_decision(
        runs_dir=tmp_path,
        pathway_id="gb_skilled_worker",
        requirement="eligibility",
        decision="reject",
        reviewer_id="tester",
        evidence_row=evidence_rows[0],
        explanation="wrong page",
    )
    assert decision.human_verified is False

    import csv
    reviews = list(csv.DictReader((tmp_path / "human_evidence_reviews.csv").open(encoding="utf-8")))
    adjusted = apply_reviews_to_readiness(evidence_rows, reviews)
    assert pathway_is_provisionally_ready(adjusted, "gb_skilled_worker") is False

    reviews_out = audit_readiness(
        evidence_rows,
        provisionally_ready=["gb_skilled_worker"],
        pathways_to_audit=["gb_skilled_worker"],
        human_reviews=reviews,
    )
    assert reviews_out[0].decision == "downgrade"
    assert reviews_out[0].human_verified is False


def test_readiness_outputs_agree():
    evidence_rows = [
        {
            "pathway_id": "nl_naturalisation",
            "requirement": req,
            "classification": "NOT_APPLICABLE" if req == "financial_or_sponsorship" else "COMPLETE",
            "document_id": "100",
            "version_id": "200",
            "source_url": "https://ind.nl/en/dutch-citizenship/becoming-a-dutch-citizen-through-naturalisation",
            "entity_type": "true_pathway",
            "passage": "Naturalisation eligibility criteria and process. " * 5,
            "country": "NL",
            "not_applicable_rationale": "Naturalisation has no sponsorship funds requirement in this inventory.",
            "explanation": "Naturalisation has no sponsorship funds requirement in this inventory.",
        }
        for req in [
            "eligibility",
            "required_documents",
            "application_process",
            "financial_or_sponsorship",
            "official_provenance",
        ]
    ]
    reviews = audit_readiness(
        evidence_rows,
        provisionally_ready=["nl_naturalisation"],
        pathways_to_audit=["nl_naturalisation"],
    )
    summary = summarize_readiness(reviews)
    assert summary.retain == 1
    assert summary.downgrade == 0
    assert summary.requires_manual_review == 0
    assert summary.provisionally_ready_pathways == ["nl_naturalisation"]
    assert summary.human_verified_pathways == []
    assert reviews[0].decision == "retain"
    assert reviews[0].human_verified is False


def test_human_verification_requires_explicit_confirm(tmp_path: Path):
    row = {
        "pathway_id": "gb_skilled_worker",
        "requirement": "eligibility",
        "classification": "COMPLETE",
        "document_id": "1",
        "passage": "x" * 60,
    }
    d = record_decision(
        runs_dir=tmp_path,
        pathway_id="gb_skilled_worker",
        requirement="eligibility",
        decision="confirm",
        reviewer_id="alice",
        evidence_row=row,
    )
    # Requirement-level confirm is recorded, but pathway human_verified
    # requires a separate confirm-pathway decision.
    assert d.decision == "confirm"
    assert d.human_verified is False
    d2 = record_decision(
        runs_dir=tmp_path,
        pathway_id="gb_skilled_worker",
        requirement="required_documents",
        decision="investigate",
        reviewer_id="alice",
        evidence_row=row,
    )
    assert d2.human_verified is False


def test_unused_unlinked_not_orphaned_integrity():
    evidence_rows = [
        {
            "pathway_id": "gb_student",
            "requirement": "eligibility",
            "classification": "UNEVALUATED",
            "document_id": "",
            "version_id": "",
            "source_url": "",
            "entity_type": "true_pathway",
        }
    ]
    mock_session = MagicMock()
    # documents query returns one extracted unused doc
    doc_rows = [
        {
            "id": 999,
            "canonical_url": "https://example.com/unused",
            "url_normalized": "https://example.com/unused",
            "title": "Unused",
            "primary_source_id": 1,
            "discovery_status": "discovered",
        }
    ]
    version_rows = [{"id": 1, "document_id": 999, "storage_path": "x", "content_sha256": "a", "is_current": True}]
    extraction_rows = [
        {"id": 1, "document_version_id": 1, "status": "success", "storage_path": "x", "content_sha256": "a"}
    ]

    def _exec(query):
        q = str(query)
        m = MagicMock()
        if "FROM documents" in q:
            m.mappings.return_value.all.return_value = doc_rows
        elif "FROM document_versions" in q:
            m.mappings.return_value.all.return_value = version_rows
        elif "FROM document_extractions" in q:
            m.mappings.return_value.all.return_value = extraction_rows
        else:
            m.mappings.return_value.all.return_value = []
        return m

    mock_session.execute.side_effect = _exec
    issues = audit_evidence_consistency(mock_session, evidence_rows, inventory_rows=[])
    types = {i["issue_type"] for i in issues}
    assert "unused_unlinked_document" in types
    assert "orphaned_document" not in types


def test_reassess_idempotent():
    rows = [
        {
            "pathway_id": "us_h1b",
            "requirement": "required_documents",
            "entity_type": "true_pathway",
            "classification": "PARTIAL",
            "document_id": "1549",
            "source_url": "https://www.uscis.gov/working-in-the-united-states/international-entrepreneur-rule",
            "passage": "Evidence Checklist " * 10,
            "method": "machine_assessed",
            "country": "US",
        }
    ]
    prior = [{"pathway_id": "us_h1b", "requirement": "required_documents"}]
    once, ch1 = reassess_suspicious_rows(rows, prior_machine_changes=prior)
    twice, ch2 = reassess_suspicious_rows(once, prior_machine_changes=prior)
    assert once[0]["classification"] == "UNEVALUATED"
    assert twice[0]["classification"] == "UNEVALUATED"
    assert len(ch1) == 1
    assert len(ch2) == 0
