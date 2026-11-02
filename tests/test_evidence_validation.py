"""Phase 6F.0 evidence validation rules and artifact safety."""

from __future__ import annotations

import hashlib
from pathlib import Path

from lifepilot.coverage.evidence_validation.decisions import build_seed_decisions
from lifepilot.coverage.evidence_validation.readiness import pathway_provisional_ready
from lifepilot.coverage.evidence_validation.reconcile import compare_matrices
from lifepilot.coverage.evidence_validation.rules import (
    cannot_auto_assign_human_verified,
    heading_alone_cannot_be_complete,
    not_applicable_requires_rationale,
    pathway_cross_contamination,
    unevaluated_must_not_become_missing_without_assessment,
    unrelated_required_documents_snippet,
)
from lifepilot.coverage.evidence_validation.decisions import FieldDecision


def test_unrelated_evidence_cannot_satisfy_required_documents():
    assert unrelated_required_documents_snippet(
        "You can apply for a visa up to 3 months before the day you are due to "
        "start work in the UK. This date is listed on your certificate of sponsorship."
    )
    assert not unrelated_required_documents_snippet(
        "When you apply you’ll need to provide: your certificate of sponsorship "
        "reference number; a valid passport"
    )


def test_generic_heading_alone_cannot_establish_complete():
    assert heading_alone_cannot_be_complete(
        heading="Eligibility and requirements",
        body="Information for applicants on eligibility and requirements",
    )
    assert heading_alone_cannot_be_complete(
        heading="Documents required",
        body="Documents required to apply for an Employment Pass.",
        min_body_chars=80,
    )
    assert not heading_alone_cannot_be_complete(
        heading="Documents you'll need to apply",
        body=(
            "When you apply you’ll need to provide: your certificate of sponsorship "
            "reference number, proof of English, a valid passport, job title and salary."
        ),
    )


def test_pathway_cross_contamination_blocked():
    assert pathway_cross_contamination(
        "These are the steps in the application process for the residence permit.",
        "nl_naturalisation",
    )
    assert not pathway_cross_contamination(
        "How to apply for naturalisation and complete the application form.",
        "nl_naturalisation",
    )


def test_unevaluated_does_not_become_missing_without_assessment():
    assert unevaluated_must_not_become_missing_without_assessment(
        "UNEVALUATED", "MISSING", assessed=False
    )
    assert not unevaluated_must_not_become_missing_without_assessment(
        "UNEVALUATED", "PARTIAL", assessed=True
    )
    # Seed decisions must not coerce HSM docs UNEVALUATED→MISSING
    seeds = {(d.pathway_id, d.requirement): d for d in build_seed_decisions()}
    hsm = seeds[("nl_highly_skilled_migrant", "required_documents")]
    assert hsm.previous_classification == "UNEVALUATED"
    assert hsm.proposed_classification == "UNEVALUATED"


def test_not_applicable_requires_rationale():
    assert not_applicable_requires_rationale(
        "NOT_APPLICABLE",
        "Financial/sponsorship not applicable to citizenship grant pathway.",
    )
    assert not not_applicable_requires_rationale("NOT_APPLICABLE", "   ")


def test_human_verification_cannot_be_assigned_automatically():
    for d in build_seed_decisions():
        assert d.reviewer_status == "automated_provisional"
        assert d.human_reviewed_classification == ""
        assert cannot_auto_assign_human_verified(d.reviewer_status)


def test_gb_required_documents_override_fixes_false_snippet():
    seeds = {(d.pathway_id, d.requirement): d for d in build_seed_decisions()}
    docs = seeds[("gb_skilled_worker", "required_documents")]
    assert "3 months before" not in docs.exact_supporting_passage.lower()
    assert "certificate of sponsorship reference number" in docs.exact_supporting_passage.lower()
    assert docs.evidence_quality_issue == "incorrect_match_snippet"


def test_ie_downgraded_from_complete_for_nav_only():
    seeds = {(d.pathway_id, d.requirement): d for d in build_seed_decisions()}
    elig = seeds[("ie_employment_permits", "eligibility")]
    assert elig.previous_classification == "COMPLETE"
    assert elig.proposed_classification == "PARTIAL"


def test_pathway_ready_requires_all_critical_complete():
    cells = [
        FieldDecision(
            review_id="t1",
            country="GB",
            pathway_id="gb_skilled_worker",
            original_pathway_id="gb_skilled_worker",
            requirement=req,
            previous_classification="COMPLETE",
            proposed_classification="COMPLETE",
            evidence_quality_issue="none",
            evidence_category="A",
            source_url="https://www.gov.uk/skilled-worker-visa",
            source_id=86,
            document_id=1,
            version_id=1,
            exact_supporting_passage="x" * 100,
            directly_answers=True,
            pathway_specific=True,
            extraction_complete_enough=True,
            explanation="ok",
            remediation_category="human_verification",
        )
        for req in (
            "eligibility",
            "required_documents",
            "application_process",
            "financial_or_sponsorship",
            "official_provenance",
        )
    ]
    assert pathway_provisional_ready(cells)
    cells[1].proposed_classification = "UNEVALUATED"
    assert not pathway_provisional_ready(cells)


def test_compare_matrices_reports_class_diffs(tmp_path: Path):
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    header = "original_pathway_id,requirement_field,coverage_class\n"
    a.write_text(
        header + "gb_skilled_worker,required_documents,COMPLETE\n",
        encoding="utf-8",
    )
    b.write_text(
        header + "gb_skilled_worker,required_documents,PARTIAL\n",
        encoding="utf-8",
    )
    result = compare_matrices(a, b)
    assert result["class_diff_count"] == 1


def test_phase_6e_artifacts_exist_unchanged_baseline():
    root = Path(__file__).resolve().parents[1] / "data" / "runs"
    for name in (
        "phase_6e_acceptance_readiness.json",
        "phase_6e1_calibrated_coverage_matrix.csv",
        "phase_6e1_review_queue.csv",
    ):
        path = root / name
        assert path.is_file(), name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert len(digest) == 64
