"""Controlled official-source discovery proposals (no unapproved fetches)."""

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
    approval_status: str  # pending_human_approval | already_in_corpus | eligible_if_in_allowlist

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def build_source_proposals() -> list[SourceProposal]:
    """Grounded candidates discovered from corpus / stored HTML only."""
    return [
        SourceProposal(
            country="SG",
            pathway_id="sg_permanent_resident",
            requirement="required_documents",
            url=(
                "https://www.ica.gov.sg/docs/ica/ica/ipses/"
                "pr-document-checklist.pdf?sfvrsn=ffa4df1c_0"
            ),
            official_authority="ICA Singapore",
            parent_page_or_origin=(
                "https://www.ica.gov.sg/information-and-e-services/"
                "citizenship-and-permanent-residence/permanent-residence/"
                "permanent-residence-application (doc 1544 raw HTML href)"
            ),
            already_approved_source=True,
            within_existing_allowlist=False,
            expected_document_type="pdf_checklist",
            reason_needed=(
                "PR application page links to official documents checklist PDF. "
                "URL is under /docs/… and outside current allowlist pattern "
                "…/permanent-residence*. Do not fetch until allowlist amendment approved."
            ),
            approval_status="pending_human_approval",
        ),
        SourceProposal(
            country="IE",
            pathway_id="ie_critical_skills_employment_permit",
            requirement="required_documents",
            url=(
                "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/"
                "employment-permits/application-forms"
            ),
            official_authority="DETE Ireland",
            parent_page_or_origin="Already ingested hub / application-forms area (doc 1555 lineage)",
            already_approved_source=True,
            within_existing_allowlist=True,
            expected_document_type="html_or_pdf_checklist",
            reason_needed=(
                "Critical Skills / General Employment Permit pages lack full supporting-"
                "document checklists; deeper approved forms pages may hold them. Prefer "
                "inspecting existing allowlisted corpus before new fetch."
            ),
            approval_status="eligible_if_in_allowlist",
        ),
        SourceProposal(
            country="IE",
            pathway_id="ie_general_employment_permit",
            requirement="required_documents",
            url=(
                "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/"
                "employment-permits/application-forms"
            ),
            official_authority="DETE Ireland",
            parent_page_or_origin="Already ingested hub / application-forms area",
            already_approved_source=True,
            within_existing_allowlist=True,
            expected_document_type="html_or_pdf_checklist",
            reason_needed=(
                "Same checklist gap as Critical Skills; permit-specific supporting "
                "documents still incomplete on type pages 2168/2169."
            ),
            approval_status="eligible_if_in_allowlist",
        ),
        SourceProposal(
            country="NL",
            pathway_id="nl_permanent_residence",
            requirement="required_documents",
            url=(
                "https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/"
                "permanent-residence-permit"
            ),
            official_authority="IND Netherlands",
            parent_page_or_origin="Current permanent residence page (doc 2216) — checklist child unknown",
            already_approved_source=True,
            within_existing_allowlist=True,
            expected_document_type="html_checklist_or_pdf_form",
            reason_needed=(
                "No itemised supporting-document checklist found on current PR page. "
                "Exact child URL not yet discovered from stored HTML."
            ),
            approval_status="pending_human_approval",
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
            parent_page_or_origin="Student 500 listing + disclaimer-for-document-checklist (doc 2240)",
            already_approved_source=True,
            within_existing_allowlist=True,
            expected_document_type="html_document_checklist_section",
            reason_needed=(
                "Need substantive Student 500 document checklist (not disclaimer/cost text). "
                "Financial evidence recovered from existing PageSchema; checklist still gap."
            ),
            approval_status="eligible_if_in_allowlist",
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
            parent_page_or_origin="How-to-apply page defers to self-check tool for eligibility",
            already_approved_source=True,
            within_existing_allowlist=True,
            expected_document_type="html_eligibility_criteria_for_grant",
            reason_needed=(
                "Need citizenship-by-grant criteria beyond self-check teaser; do not use "
                "birth/descent pages as substitutes."
            ),
            approval_status="pending_human_approval",
        ),
        SourceProposal(
            country="NL",
            pathway_id="nl_highly_skilled_migrant",
            requirement="required_documents",
            url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant",
            official_authority="IND Netherlands",
            parent_page_or_origin="HSM page doc 150 / written application forms links",
            already_approved_source=True,
            within_existing_allowlist=True,
            expected_document_type="pdf_form_checklist",
            reason_needed=(
                "HSM page references forms but lacks a full itemised applicant document "
                "checklist; MVV passport crumb alone rejected."
            ),
            approval_status="eligible_if_in_allowlist",
        ),
    ]
