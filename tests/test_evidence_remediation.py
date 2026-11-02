"""Phase 6F.1 evidence remediation rules and readiness."""

from __future__ import annotations

import csv
import html as html_mod
import json
from pathlib import Path

from lifepilot.coverage.evidence_remediation.assessments import build_assessments, pathway_ready
from lifepilot.coverage.evidence_remediation.proposals import build_source_proposals
from lifepilot.coverage.evidence_remediation.readiness import (
    PREVIOUSLY_PROVISIONAL_READY_6F0,
    recalculate_readiness,
)
from lifepilot.coverage.evidence_remediation.report import default_6f1_paths, write_assessments_csv
from lifepilot.coverage.evidence_validation.rules import (
    cannot_auto_assign_human_verified,
    heading_alone_cannot_be_complete,
    not_applicable_requires_rationale,
    pathway_cross_contamination,
)
from lifepilot.extract.html_structured import extract_html_document
from lifepilot.registry.urls import url_in_any_allowed_pattern


def _by_key(assessments):
    return {(a.pathway_id, a.requirement): a for a in assessments}


def test_gb_required_documents_points_to_checklist_not_timing():
    a = _by_key(build_assessments())[("gb_skilled_worker", "required_documents")]
    assert a.classification == "COMPLETE"
    assert "3 months before" not in a.passage.lower()
    assert "certificate of sponsorship" in a.passage.lower() or "cos" in a.passage.lower()
    assert "documents" in a.source_url


def test_nl_naturalisation_uses_dutch_pdf_not_residence_permit():
    a = _by_key(build_assessments())
    docs = a[("nl_naturalisation", "required_documents")]
    proc = a[("nl_naturalisation", "application_process")]
    assert docs.classification == "COMPLETE"
    assert docs.document_id == 2249
    assert "naturalisatie" in docs.passage.lower() or "documenten" in docs.passage.lower()
    assert proc.classification == "COMPLETE"
    assert not pathway_cross_contamination(proc.passage, "nl_naturalisation")


def test_nz_citizenship_scoped_to_grant_not_descent():
    a = _by_key(build_assessments())[("nz_citizenship_by_grant", "eligibility")]
    assert a.pathway_id == "nz_citizenship_by_grant"
    assert a.classification == "PARTIAL"
    assert "descent" not in a.passage.lower() or "birth, descent and grant" in a.passage.lower()
    assert a.evidence_repair == "normalized_split"


def test_ie_hub_not_complete_and_permit_split():
    assessments = build_assessments()
    by = _by_key(assessments)
    hub = by[("ie_employment_permits_hub", "eligibility")]
    assert hub.entity_type == "information_hub"
    assert hub.classification == "NOT_APPLICABLE"
    assert not_applicable_requires_rationale(
        hub.classification, hub.not_applicable_rationale
    )
    csep = by[("ie_critical_skills_employment_permit", "eligibility")]
    gep = by[("ie_general_employment_permit", "eligibility")]
    assert csep.classification == "COMPLETE"
    assert gep.classification == "COMPLETE"
    assert by[("ie_critical_skills_employment_permit", "required_documents")].classification == (
        "PARTIAL"
    )


def test_heading_only_evidence_rejected_for_sg_style():
    assert heading_alone_cannot_be_complete(
        heading="Documents required",
        body="Documents required to apply for an Employment Pass.",
        min_body_chars=80,
    )
    docs = _by_key(build_assessments())[("sg_employment_pass", "required_documents")]
    assert docs.classification == "COMPLETE"
    assert "passport" in docs.passage.lower()
    assert len(docs.passage) > 80


def test_invalid_complete_downgraded_au_ca_sgpr():
    by = _by_key(build_assessments(au_financial_improved=True))
    assert by[("au_student_500", "required_documents")].classification == "PARTIAL"
    assert by[("au_student_500", "required_documents")].previous_6f0 == "COMPLETE"
    assert by[("ca_express_entry", "eligibility")].classification == "PARTIAL"
    assert by[("sg_permanent_resident", "financial_or_sponsorship")].classification == (
        "PARTIAL"
    )
    assert by[("sg_permanent_resident", "financial_or_sponsorship")].previous_6f0 == (
        "NOT_APPLICABLE"
    )


def test_not_applicable_rationale_enforced():
    by = _by_key(build_assessments())
    for key in (
        ("nl_naturalisation", "financial_or_sponsorship"),
        ("nz_citizenship_by_grant", "financial_or_sponsorship"),
    ):
        a = by[key]
        assert a.classification == "NOT_APPLICABLE"
        assert not_applicable_requires_rationale(
            a.classification, a.not_applicable_rationale
        )


def test_cross_pathway_contamination_guard():
    assert pathway_cross_contamination(
        "These are the steps in the application process for the residence permit.",
        "nl_naturalisation",
    )
    proc = _by_key(build_assessments())[("nl_naturalisation", "application_process")]
    assert not pathway_cross_contamination(proc.passage, "nl_naturalisation")


def test_singapore_checklist_and_ica_pdf_not_claimed_ingested():
    by = _by_key(build_assessments())
    ep = by[("sg_employment_pass", "required_documents")]
    assert ep.document_id == 2031
    assert ep.classification == "COMPLETE"
    pr = by[("sg_permanent_resident", "required_documents")]
    assert pr.classification == "PARTIAL"
    assert "pdf" in pr.passage.lower()
    proposals = build_source_proposals()
    ica = next(p for p in proposals if p.pathway_id == "sg_permanent_resident")
    assert "pr-document-checklist.pdf" in ica.url
    assert ica.within_existing_allowlist is False
    assert not url_in_any_allowed_pattern(
        ica.url,
        [
            "https://www.ica.gov.sg/information-and-e-services/"
            "citizenship-and-permanent-residence/permanent-residence*"
        ],
    )


def test_australia_pageschema_financial_merge_on_non_thin_main():
    """Embedded PageSchema must merge when main lacks financial guidance."""
    payload = {
        "title": "Student visa",
        "sectionMain": (
            "<h2>How to apply</h2><p>Create an ImmiAccount and lodge online.</p>"
            "<h2>Eligibility</h2><p>You must be enrolled and meet Genuine Student.</p>"
        ),
        "sectionFinance": (
            "<h2>Gather documents</h2>"
            "<p>Evidence you have enough money for your stay including financial capacity.</p>"
            "<ul><li>12 months of funds</li><li>Sponsorship evidence if applicable</li></ul>"
        ),
    }
    escaped = html_mod.escape(json.dumps(payload), quote=True)
    html = f"""<!DOCTYPE html>
<html><head><title>Student visa</title></head>
<body>
<main id="content" role="main">
  <h1>Student visa</h1>
  <h2>How to apply</h2>
  <p>Create an ImmiAccount and lodge online. This main content is intentionally
  long enough that the extractor treats the DOM as non-thin while still omitting
  the financial capacity guidance that only exists in embedded CMS JSON.</p>
  <h2>Eligibility</h2>
  <p>You must be enrolled and meet Genuine Student requirements for subclass 500.</p>
</main>
<input type="hidden" value="{escaped}" />
</body></html>
""".encode()
    doc = extract_html_document(html)
    blob = " ".join(
        (b.text or "") + " " + " ".join(b.items or ()) for b in doc.blocks
    ).lower()
    assert "enough money" in blob or "financial capacity" in blob
    assert "gather documents" in blob
    assert "genuine student" in blob


def test_readiness_recompute_deterministic():
    assessments = build_assessments(au_financial_improved=True)
    r1 = recalculate_readiness(assessments)
    r2 = recalculate_readiness(assessments)
    assert r1["provisional_ready_after_6f1"] == r2["provisional_ready_after_6f1"]
    assert "gb_skilled_worker" in r1["provisional_ready_after_6f1"]
    assert "nl_naturalisation" in r1["newly_provisionally_ready"]
    assert "sg_employment_pass" in r1["newly_provisionally_ready"]
    assert r1["human_verified_ready_pathways"] == []
    assert list(PREVIOUSLY_PROVISIONAL_READY_6F0) == r1["provisional_ready_before_6f1"]
    for a in assessments:
        assert a.reviewer_status == "automated_provisional"
        assert cannot_auto_assign_human_verified(a.reviewer_status)


def test_pathway_ready_requires_all_applicable_complete():
    assessments = build_assessments()
    by_pid: dict[str, list] = {}
    for a in assessments:
        by_pid.setdefault(a.pathway_id, []).append(a)
    assert pathway_ready(by_pid["gb_skilled_worker"])
    assert pathway_ready(by_pid["nl_naturalisation"])
    assert pathway_ready(by_pid["sg_employment_pass"])
    assert not pathway_ready(by_pid["au_student_500"])
    assert not pathway_ready(by_pid["nz_citizenship_by_grant"])


def test_report_csv_roundtrip(tmp_path: Path):
    assessments = build_assessments(au_financial_improved=True)
    paths = default_6f1_paths(tmp_path)
    write_assessments_csv(paths["evidence_repairs"], assessments)
    with paths["evidence_repairs"].open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(assessments)
    assert {r["pathway_id"] for r in rows} >= {
        "gb_skilled_worker",
        "nl_naturalisation",
        "nz_citizenship_by_grant",
        "sg_employment_pass",
    }


def test_provenance_fields_present_for_complete():
    for a in build_assessments(au_financial_improved=True):
        if a.classification != "COMPLETE":
            continue
        assert a.source_id
        assert a.source_url.startswith("http")
        assert a.passage.strip()
        assert a.explanation.strip()
        # document_id may be absent only if explicitly None for incomplete corpus links
        if a.pathway_id in {
            "gb_skilled_worker",
            "nl_naturalisation",
            "sg_employment_pass",
        }:
            assert a.document_id
