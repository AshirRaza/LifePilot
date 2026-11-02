"""Source proposals awaiting human approval (no unapproved fetches)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class SourceProposal:
    country: str
    pathway_id: str
    requirement: str
    url: str
    official_authority: str
    parent_page_or_origin: str
    already_approved_source: bool
    within_existing_allowlist: bool
    expected_document_type: str
    reason_needed: str
    approval_status: str

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def build_source_proposals(*, ica_ingested: bool) -> list[SourceProposal]:
    rows: list[SourceProposal] = []
    if not ica_ingested:
        rows.append(
            SourceProposal(
                country="SG",
                pathway_id="sg_permanent_resident",
                requirement="required_documents",
                url="https://www.ica.gov.sg/docs/ica/ica/ipses/pr-document-checklist.pdf",
                official_authority="ICA Singapore",
                parent_page_or_origin="doc 1544 HTML href",
                already_approved_source=True,
                within_existing_allowlist=True,
                expected_document_type="pdf_checklist",
                reason_needed="ICA checklist PDF authorized in 6F.2 but not yet ingested.",
                approval_status="approved_exact_url",
            )
        )

    rows.extend(
        [
            SourceProposal(
                country="IE",
                pathway_id="ie_critical_skills_employment_permit",
                requirement="required_documents",
                url=(
                    "https://enterprise.gov.ie/en/publications/publication-files/"
                    "critical-skills-employment-permits-checklist.pdf"
                ),
                official_authority="DETE Ireland",
                parent_page_or_origin="Linked from ingested Critical Skills page doc 2169",
                already_approved_source=True,
                within_existing_allowlist=False,
                expected_document_type="pdf_checklist",
                reason_needed=(
                    "Permit-specific checklist PDF is under /en/publications/… outside "
                    "employment-permits* allowlist. Exact URL approval required."
                ),
                approval_status="pending_human_approval",
            ),
            SourceProposal(
                country="IE",
                pathway_id="ie_general_employment_permit",
                requirement="required_documents",
                url=(
                    "https://enterprise.gov.ie/en/Publications/Publication-files/"
                    "General-Employment-Permits-Checklist.pdf"
                ),
                official_authority="DETE Ireland",
                parent_page_or_origin="Linked from ingested General Employment Permit page doc 2168",
                already_approved_source=True,
                within_existing_allowlist=False,
                expected_document_type="pdf_checklist",
                reason_needed="Same allowlist gap as Critical Skills checklist PDF.",
                approval_status="pending_human_approval",
            ),
            SourceProposal(
                country="IE",
                pathway_id="ie_employment_permits_hub",
                requirement="required_documents",
                url="https://enterprise.gov.ie/en/Publications/Employment-Permits-Checklists.html",
                official_authority="DETE Ireland",
                parent_page_or_origin="Linked from application-forms doc 1555",
                already_approved_source=True,
                within_existing_allowlist=False,
                expected_document_type="html_checklist_index",
                reason_needed="Checklist index page outside employment-permits allowlist.",
                approval_status="pending_human_approval",
            ),
            SourceProposal(
                country="NL",
                pathway_id="nl_permanent_residence",
                requirement="required_documents",
                url="https://ind.nl/en/forms/6009.pdf",
                official_authority="IND Netherlands",
                parent_page_or_origin="Linked from permanent residence page doc 2216 raw HTML",
                already_approved_source=True,
                within_existing_allowlist=False,
                expected_document_type="pdf_application_form",
                reason_needed=(
                    "Written application form for permanent residence / EU long-term resident; "
                    "outside permanent-residence-permit* path prefix."
                ),
                approval_status="pending_human_approval",
            ),
            SourceProposal(
                country="NL",
                pathway_id="nl_permanent_residence",
                requirement="financial_or_sponsorship",
                url="https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/permanent-residence-permit",
                official_authority="IND Netherlands",
                parent_page_or_origin="Income requirements link referenced from requirements list",
                already_approved_source=True,
                within_existing_allowlist=True,
                expected_document_type="html_income_requirements_child",
                reason_needed="Need exact income-threshold child URL if separate from main PR page.",
                approval_status="pending_human_approval",
            ),
            SourceProposal(
                country="NZ",
                pathway_id="nz_citizenship_by_grant",
                requirement="eligibility",
                url=(
                    "https://www.govt.nz/browse/passports-citizenship-and-identity/"
                    "nz-citizenship/how-to-apply-for-nz-citizenship/"
                ),
                official_authority="DIA / New Zealand Government",
                parent_page_or_origin="How-to-apply still defers to self-check tool",
                already_approved_source=True,
                within_existing_allowlist=True,
                expected_document_type="html_grant_eligibility_criteria",
                reason_needed="Substantive grant eligibility criteria page beyond self-check teaser.",
                approval_status="pending_human_approval",
            ),
            SourceProposal(
                country="CA",
                pathway_id="ca_express_entry",
                requirement="eligibility",
                url=(
                    "https://www.canada.ca/en/immigration-refugees-citizenship/services/"
                    "immigrate-canada/express-entry/who-can-apply.html"
                ),
                official_authority="IRCC",
                parent_page_or_origin="Already ingested who-can-apply page documents program split",
                already_approved_source=True,
                within_existing_allowlist=True,
                expected_document_type="pathway_normalization_proposal",
                reason_needed=(
                    "Propose normalizing ca_express_entry into information hub + "
                    "ca_federal_skilled_worker / ca_canadian_experience_class / "
                    "ca_federal_skilled_trades pathways (docs 2057/2056/2137 already ingested)."
                ),
                approval_status="normalization_proposal",
            ),
            SourceProposal(
                country="AU",
                pathway_id="au_student_500",
                requirement="required_documents",
                url=(
                    "https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/"
                    "student-500"
                ),
                official_authority="Department of Home Affairs",
                parent_page_or_origin="Student 500 listing refers to document checklist tool/ImmiAccount",
                already_approved_source=True,
                within_existing_allowlist=True,
                expected_document_type="html_or_tool_checklist",
                reason_needed=(
                    "Need a stable official checklist artifact; financial thresholds already "
                    "recovered from doc 2218."
                ),
                approval_status="pending_human_approval",
            ),
            SourceProposal(
                country="NL",
                pathway_id="nl_student_residence",
                requirement="required_documents",
                url=(
                    "https://ind.nl/en/residence-permits/study/"
                    "student-residence-permit-for-university-or-higher-professional-education"
                ),
                official_authority="IND Netherlands",
                parent_page_or_origin="doc 2212 — forms/annex checklist child unknown",
                already_approved_source=True,
                within_existing_allowlist=True,
                expected_document_type="pdf_form_checklist",
                reason_needed="Itemised student supporting-document checklist still missing.",
                approval_status="pending_human_approval",
            ),
        ]
    )
    return rows
