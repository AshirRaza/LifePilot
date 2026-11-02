"""Phase 6F.2 approved-source gap closure tests."""

from __future__ import annotations

import csv
from pathlib import Path

from lifepilot.coverage.source_gap_closure.assessments import build_assessments, pathway_ready
from lifepilot.coverage.source_gap_closure.proposals import build_source_proposals
from lifepilot.coverage.source_gap_closure.readiness import (
    PREVIOUSLY_PROVISIONAL_READY_6F1,
    recalculate_readiness,
)
from lifepilot.coverage.source_gap_closure.report import default_6f2_paths, write_assessments_csv
from lifepilot.coverage.evidence_validation.rules import (
    cannot_auto_assign_human_verified,
    heading_alone_cannot_be_complete,
    not_applicable_requires_rationale,
)
from lifepilot.registry.urls import url_in_any_allowed_pattern


def _by(assessments):
    return {(a.pathway_id, a.requirement): a for a in assessments}


def test_ica_exact_url_not_docs_wildcard():
    proposals = build_source_proposals(ica_ingested=True)
    assert all(
        not (p.url.endswith("*") and "/docs/" in p.url) for p in proposals
    )
    # Exact ICA PDF matches only exact pattern, not PR path prefix
    ica = "https://www.ica.gov.sg/docs/ica/ica/ipses/pr-document-checklist.pdf"
    assert url_in_any_allowed_pattern(
        ica,
        [
            "https://www.ica.gov.sg/information-and-e-services/citizenship-and-permanent-residence/permanent-residence*",
            ica,
        ],
    )
    assert not url_in_any_allowed_pattern(
        ica,
        [
            "https://www.ica.gov.sg/information-and-e-services/citizenship-and-permanent-residence/permanent-residence*"
        ],
    )


def test_sg_pr_docs_complete_only_with_ica_doc():
    without = _by(build_assessments(ica_doc_id=None))[
        ("sg_permanent_resident", "required_documents")
    ]
    with_doc = _by(build_assessments(ica_doc_id=2252))[
        ("sg_permanent_resident", "required_documents")
    ]
    assert without.classification == "PARTIAL"
    assert with_doc.classification == "COMPLETE"
    assert "passport" in with_doc.passage.lower()
    assert with_doc.document_id == 2252
    # Pathway must not auto-ready while financial remains PARTIAL
    by_pid = {}
    for a in build_assessments(ica_doc_id=2252):
        by_pid.setdefault(a.pathway_id, []).append(a)
    assert not pathway_ready(by_pid["sg_permanent_resident"])


def test_hsm_form_7511_document_list_promoted():
    a = _by(build_assessments())[("nl_highly_skilled_migrant", "required_documents")]
    assert a.classification == "COMPLETE"
    assert a.document_id == 334
    assert "passport" in a.passage.lower()
    assert "enclose" in a.passage.lower()
    assert not heading_alone_cannot_be_complete(
        heading="Means of evidence and documents",
        body=a.passage,
        min_body_chars=80,
    )


def test_ie_checklist_pdfs_remain_proposals_out_of_allowlist():
    proposals = build_source_proposals(ica_ingested=True)
    ie = [p for p in proposals if p.country == "IE" and "checklist" in p.url.lower()]
    assert len(ie) >= 2
    assert all(p.within_existing_allowlist is False for p in ie)
    assert all(p.approval_status == "pending_human_approval" for p in ie)
    docs = _by(build_assessments())[
        ("ie_critical_skills_employment_permit", "required_documents")
    ]
    assert docs.classification == "PARTIAL"


def test_au_financial_thresholds_not_keyword_only():
    a = _by(build_assessments())[("au_student_500", "financial_or_sponsorship")]
    assert a.classification == "COMPLETE"
    assert "AUD29,710" in a.passage or "29,710" in a.passage
    assert "AUD87,856" in a.passage or "87,856" in a.passage
    docs = _by(build_assessments())[("au_student_500", "required_documents")]
    assert docs.classification == "PARTIAL"


def test_ca_funds_complete_and_eligibility_not_collapsed():
    by = _by(build_assessments())
    funds = by[("ca_express_entry", "financial_or_sponsorship")]
    elig = by[("ca_express_entry", "eligibility")]
    assert funds.classification == "COMPLETE"
    assert "15,263" in funds.passage
    assert elig.classification == "PARTIAL"
    assert "3 programs" in elig.passage.lower() or "federal skilled" in elig.passage.lower()
    readiness = recalculate_readiness(build_assessments(ica_doc_id=2252))
    assert any(
        c.get("pathway_id") == "ca_express_entry"
        for c in readiness["pathway_normalization_concerns"]
    )


def test_nl_pr_financial_not_na_from_missing_info():
    a = _by(build_assessments())[("nl_permanent_residence", "financial_or_sponsorship")]
    assert a.classification == "PARTIAL"
    assert a.classification != "NOT_APPLICABLE"
    assert "income requirements" in a.passage.lower()


def test_nl_student_financial_promoted_via_sponsor_income():
    a = _by(build_assessments())[("nl_student_residence", "financial_or_sponsorship")]
    assert a.classification == "COMPLETE"
    assert "recognised" in a.passage.lower() or "recognized" in a.passage.lower()
    assert "income" in a.passage.lower()


def test_nz_grant_eligibility_rejects_self_check_as_complete():
    a = _by(build_assessments())[("nz_citizenship_by_grant", "eligibility")]
    assert a.classification == "PARTIAL"
    assert "self-check" in a.passage.lower()


def test_not_applicable_still_requires_rationale():
    by = _by(build_assessments())
    for key in (
        ("nl_naturalisation", "financial_or_sponsorship"),
        ("nz_citizenship_by_grant", "financial_or_sponsorship"),
    ):
        a = by[key]
        assert not_applicable_requires_rationale(
            a.classification, a.not_applicable_rationale
        )


def test_readiness_includes_hsm_and_retains_6f1():
    assessments = build_assessments(ica_doc_id=2252)
    readiness = recalculate_readiness(assessments)
    for pid in PREVIOUSLY_PROVISIONAL_READY_6F1:
        assert pid in readiness["provisional_ready_after_6f2"]
    assert "nl_highly_skilled_migrant" in readiness["newly_provisionally_ready"]
    assert readiness["human_verified_ready_pathways"] == []
    for a in assessments:
        assert a.reviewer_status == "automated_provisional"
        assert cannot_auto_assign_human_verified(a.reviewer_status)


def test_no_unauthorized_ie_nl_urls_marked_ingested():
    assessments = build_assessments(ica_doc_id=2252)
    urls = {a.source_url for a in assessments if a.classification == "COMPLETE"}
    assert not any("6009.pdf" in u for u in urls)
    assert not any("critical-skills-employment-permits-checklist" in u for u in urls)


def test_report_csv_roundtrip(tmp_path: Path):
    assessments = build_assessments(ica_doc_id=2252)
    paths = default_6f2_paths(tmp_path)
    write_assessments_csv(paths["evidence_repairs"], assessments)
    with paths["evidence_repairs"].open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(assessments)
