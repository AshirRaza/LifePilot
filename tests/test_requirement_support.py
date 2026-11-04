"""Requirement-level passage support regressions."""

from __future__ import annotations

from lifepilot.coverage.corpus_reliability.assessment import (
    assess_document_for_pathway,
    validate_existing_association,
)
from lifepilot.coverage.corpus_reliability.evidence_integrity import (
    revalidate_machine_upgrades,
)
from lifepilot.coverage.corpus_reliability.requirement_support import (
    passage_supports_requirement,
)


def test_cec_proof_of_funds_is_not_required_documents():
    passage = (
        "Who needs proof of funds. Proof of funds is how you show us that you have "
        "enough money to settle in Canada. Who doesn’t need proof of funds — "
        "You don’t need proof of funds if you’re applying under the Canadian Experience Class."
    )
    url = (
        "https://www.canada.ca/en/immigration-refugees-citizenship/services/"
        "immigrate-canada/express-entry/documents/proof-funds.html"
    )
    support = passage_supports_requirement(
        pathway_id="ca_canadian_experience_class",
        requirement="required_documents",
        passage=passage,
        source_url=url,
    )
    assert support.ok is False
    assert support.kind in {"mismatch", "exemption"}
    assert support.suggested_requirement == "financial_or_sponsorship"


def test_cec_exemption_as_financial_not_applicable():
    passage = (
        "Who doesn’t need proof of funds. You don’t need proof of funds if you’re "
        "applying under the Canadian Experience Class (upload a letter if asked)."
    )
    support = passage_supports_requirement(
        pathway_id="ca_canadian_experience_class",
        requirement="financial_or_sponsorship",
        passage=passage,
        source_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry/documents/proof-funds.html",
    )
    assert support.ok is True
    assert support.kind == "exemption"
    assert support.suggested_classification == "NOT_APPLICABLE"


def test_fsw_proof_of_funds_misfiled_under_documents():
    passage = (
        "Who needs proof of funds. Proof of funds is how you show us that you have "
        "enough money to settle in Canada under the Federal Skilled Worker Program."
    )
    support = passage_supports_requirement(
        pathway_id="ca_federal_skilled_worker",
        requirement="required_documents",
        passage=passage,
        source_url="https://www.canada.ca/.../proof-funds.html",
    )
    assert support.ok is False
    assert "proof_of_funds" in support.reason or support.suggested_requirement == "financial_or_sponsorship"


def test_e2_cnmi_rejected_for_treaty_investor_pathway():
    url = "https://www.uscis.gov/working-in-the-united-states/temporary-workers/e-2-visa-cnmi-only-investors"
    passage = (
        "General Qualifications for an E-2 CNMI Investor. To qualify for E-2 CNMI Investor "
        "status, you must have invested a substantial amount of capital."
    )
    support = passage_supports_requirement(
        pathway_id="us_e2",
        requirement="eligibility",
        passage=passage,
        source_url=url,
    )
    assert support.ok is False
    assert support.kind == "wrong_category"


def test_gb_student_money_passage_not_eligibility():
    passage = (
        "Money to support yourself (financial requirement). You’ll need to show you have "
        "enough money to support yourself — course fee for 1 academic year."
    )
    support = passage_supports_requirement(
        pathway_id="gb_student",
        requirement="eligibility",
        passage=passage,
        source_url="https://www.gov.uk/student-visa",
    )
    assert support.ok is False


def test_url_alone_does_not_upgrade_without_requirement_support():
    blocks = [
        {"type": "heading", "text": "Overview"},
        {
            "type": "paragraph",
            "text": "Welcome to the Student visa page. Contact us for more information about studying. " * 5,
        },
    ]
    assessment = assess_document_for_pathway(
        blocks,
        "gb_student",
        "eligibility",
        1,
        2,
        "https://www.gov.uk/student-visa",
        title="Student visa",
    )
    assert assessment is None


def test_revalidate_rejects_cec_required_documents_upgrade():
    rows = [
        {
            "pathway_id": "ca_canadian_experience_class",
            "requirement": "required_documents",
            "entity_type": "true_pathway",
            "classification": "PARTIAL",
            "document_id": "2142",
            "source_url": "https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry/documents/proof-funds.html",
            "passage": (
                "Who needs proof of funds. Proof of funds is how you show us that you have "
                "enough money. Who doesn’t need proof of funds if Canadian Experience Class."
            ),
            "method": "pathway_aware_machine",
            "country": "CA",
        },
        {
            "pathway_id": "ca_canadian_experience_class",
            "requirement": "financial_or_sponsorship",
            "entity_type": "true_pathway",
            "classification": "UNEVALUATED",
            "document_id": "",
            "source_url": "",
            "passage": "",
            "country": "CA",
        },
    ]
    prior = [
        {
            "pathway_id": "ca_canadian_experience_class",
            "requirement": "required_documents",
            "action": "pathway_aware_upgrade",
            "method": "pathway_aware_machine",
        }
    ]
    updated, changes = revalidate_machine_upgrades(rows, prior)
    docs_cell = next(r for r in updated if r["requirement"] == "required_documents")
    fin_cell = next(r for r in updated if r["requirement"] == "financial_or_sponsorship")
    assert docs_cell["classification"] == "UNEVALUATED"
    assert fin_cell["classification"] == "NOT_APPLICABLE"
    assert any(c["action"] == "reject_requirement_mismatch" for c in changes)
    assert any(c["action"] == "record_exemption_not_applicable" for c in changes)


def test_validate_existing_association_uses_requirement_support():
    ok, reason = validate_existing_association(
        pathway_id="us_e2",
        requirement="eligibility",
        source_url="https://www.uscis.gov/working-in-the-united-states/temporary-workers/e-2-visa-cnmi-only-investors",
        passage="General Qualifications for an E-2 CNMI Investor. To qualify for E-2 CNMI Investor status you must. " * 2,
        classification="PARTIAL",
    )
    assert ok is False
    assert "category" in reason or "cnmi" in reason.lower()
