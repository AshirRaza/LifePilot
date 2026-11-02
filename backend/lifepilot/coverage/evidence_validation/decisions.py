"""Deterministic automated-provisional review decisions for Phase 6F.0.

These are NOT human_verified. Passages were inspected against stored extractions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class EvidenceRefDetail:
    document_id: int
    version_id: int | None
    source_id: int
    source_url: str
    passage: str


@dataclass
class FieldDecision:
    review_id: str
    country: str
    pathway_id: str  # normalized id
    original_pathway_id: str
    requirement: str
    previous_classification: str
    proposed_classification: str
    evidence_quality_issue: str
    # A|B|C|D category from the brief
    evidence_category: str
    source_url: str
    source_id: int
    document_id: int | None
    version_id: int | None
    exact_supporting_passage: str
    directly_answers: bool
    pathway_specific: bool
    extraction_complete_enough: bool
    explanation: str
    remediation_category: str
    reviewer_status: str = "automated_provisional"
    human_reviewed_classification: str = ""
    extra_evidence: list[EvidenceRefDetail] = field(default_factory=list)
    uncertainty: str = ""

    def to_csv_row(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "country": self.country,
            "pathway": self.pathway_id,
            "requirement": self.requirement,
            "previous_classification": self.previous_classification,
            "proposed_classification": self.proposed_classification,
            "evidence_quality_issue": self.evidence_quality_issue,
            "evidence_category": self.evidence_category,
            "source_url": self.source_url,
            "source_id": self.source_id,
            "document_id": self.document_id or "",
            "version_id": self.version_id or "",
            "exact_supporting_passage": self.exact_supporting_passage,
            "directly_answers": self.directly_answers,
            "pathway_specific": self.pathway_specific,
            "extraction_complete_enough": self.extraction_complete_enough,
            "explanation": self.explanation,
            "remediation_category": self.remediation_category,
            "reviewer_status": self.reviewer_status,
            "human_reviewed_classification": self.human_reviewed_classification,
            "uncertainty": self.uncertainty,
            "extra_evidence_count": len(self.extra_evidence),
        }


READY_PATHWAYS = (
    "gb_skilled_worker",
    "nl_naturalisation",
    "nz_citizenship",
    "ie_employment_permits",
)

NEAR_READY_PATHWAYS = (
    "sg_employment_pass",
    "sg_permanent_resident",
    "ca_express_entry",
    "nl_highly_skilled_migrant",
    "nl_permanent_residence",
    "nl_student_residence",
    "au_student_500",
)

CRITICAL_FIELDS = (
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
)


def build_seed_decisions() -> list[FieldDecision]:
    """Curated provisional decisions from inspecting stored extractions."""
    d: list[FieldDecision] = []

    def add(**kwargs: Any) -> None:
        n = len(d) + 1
        kwargs.setdefault("review_id", f"F0-{n:03d}")
        kwargs.setdefault("reviewer_status", "automated_provisional")
        kwargs.setdefault("human_reviewed_classification", "")
        d.append(FieldDecision(**kwargs))

    # --- GB Skilled Worker ---
    add(
        country="GB",
        pathway_id="gb_skilled_worker",
        original_pathway_id="gb_skilled_worker",
        requirement="eligibility",
        previous_classification="COMPLETE",
        proposed_classification="COMPLETE",
        evidence_quality_issue="none",
        evidence_category="A",
        source_url="https://www.gov.uk/skilled-worker-visa/print",
        source_id=86,
        document_id=160,
        version_id=None,
        exact_supporting_passage=(
            "Eligibility / Your job — To qualify for a Skilled Worker visa, you must: "
            "work for a UK employer that’s been approved by the Home Office; have a "
            "certificate of sponsorship (CoS); do an eligible job; be paid a minimum salary."
        ),
        directly_answers=True,
        pathway_specific=True,
        extraction_complete_enough=True,
        explanation="Substantive eligibility criteria with list items on official Skilled Worker page.",
        remediation_category="human_verification",
    )
    add(
        country="GB",
        pathway_id="gb_skilled_worker",
        original_pathway_id="gb_skilled_worker",
        requirement="required_documents",
        previous_classification="COMPLETE",
        proposed_classification="COMPLETE",
        evidence_quality_issue="incorrect_match_snippet",
        evidence_category="A",
        source_url="https://www.gov.uk/skilled-worker-visa/documents-you-must-provide",
        source_id=86,
        document_id=155,
        version_id=None,
        exact_supporting_passage=(
            "Documents you'll need to apply — When you apply you’ll need to provide: "
            "your certificate of sponsorship reference number; proof of your knowledge of English; "
            "a valid passport; your job title and annual salary; occupation code; employer name "
            "and sponsor licence number."
        ),
        directly_answers=True,
        pathway_specific=True,
        extraction_complete_enough=True,
        explanation=(
            "6E.1 cited 'apply up to 3 months before…' which is timing, not documents. "
            "Correct documents list exists on documents-you-must-provide page (doc 155)."
        ),
        remediation_category="correct_evidence_selection",
        uncertainty="Prior COMPLETE used unrelated snippet; corrected passage supports COMPLETE provisionally.",
    )
    add(
        country="GB",
        pathway_id="gb_skilled_worker",
        original_pathway_id="gb_skilled_worker",
        requirement="application_process",
        previous_classification="COMPLETE",
        proposed_classification="COMPLETE",
        evidence_quality_issue="none",
        evidence_category="A",
        source_url="https://www.gov.uk/skilled-worker-visa/print",
        source_id=86,
        document_id=160,
        version_id=None,
        exact_supporting_passage=(
            "How to apply — You must apply online. How you apply depends on whether you’re "
            "outside the UK, extending, or switching."
        ),
        directly_answers=True,
        pathway_specific=True,
        extraction_complete_enough=True,
        explanation="Application steps present with online apply instruction.",
        remediation_category="human_verification",
    )
    add(
        country="GB",
        pathway_id="gb_skilled_worker",
        original_pathway_id="gb_skilled_worker",
        requirement="financial_or_sponsorship",
        previous_classification="COMPLETE",
        proposed_classification="COMPLETE",
        evidence_quality_issue="incorrect_match_snippet",
        evidence_category="A",
        source_url="https://www.gov.uk/skilled-worker-visa/print",
        source_id=86,
        document_id=160,
        version_id=None,
        exact_supporting_passage=(
            "Eligibility list requires certificate of sponsorship; costs section requires "
            "prove you have enough personal savings; documents page lists bank statements "
            "unless employer supports you via CoS."
        ),
        directly_answers=True,
        pathway_specific=True,
        extraction_complete_enough=True,
        explanation=(
            "Prior snippet was application timing mentioning CoS. Real sponsorship/funds "
            "evidence exists across eligibility, costs, and documents pages."
        ),
        remediation_category="correct_evidence_selection",
    )
    add(
        country="GB",
        pathway_id="gb_skilled_worker",
        original_pathway_id="gb_skilled_worker",
        requirement="official_provenance",
        previous_classification="COMPLETE",
        proposed_classification="COMPLETE",
        evidence_quality_issue="none",
        evidence_category="A",
        source_url="https://www.gov.uk/skilled-worker-visa",
        source_id=86,
        document_id=99,
        version_id=None,
        exact_supporting_passage="Official GOV.UK Skilled Worker visa guidance (approved source 86).",
        directly_answers=True,
        pathway_specific=True,
        extraction_complete_enough=True,
        explanation="Approved Home Office source with successful extractions.",
        remediation_category="human_verification",
    )

    # --- NL naturalisation ---
    add(
        country="NL",
        pathway_id="nl_naturalisation",
        original_pathway_id="nl_naturalisation",
        requirement="eligibility",
        previous_classification="COMPLETE",
        proposed_classification="COMPLETE",
        evidence_quality_issue="none",
        evidence_category="A",
        source_url="https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation",
        source_id=11040,
        document_id=2235,
        version_id=None,
        exact_supporting_passage=(
            "Requirements — The following requirements apply to naturalisation: age 18+; "
            "identity documents; 5 consecutive years residence; valid residence status; "
            "civic integration exam A2 (with exemptions)."
        ),
        directly_answers=True,
        pathway_specific=True,
        extraction_complete_enough=True,
        explanation="Detailed naturalisation requirements list on IND page.",
        remediation_category="human_verification",
    )
    add(
        country="NL",
        pathway_id="nl_naturalisation",
        original_pathway_id="nl_naturalisation",
        requirement="required_documents",
        previous_classification="COMPLETE",
        proposed_classification="PARTIAL",
        evidence_quality_issue="insufficient_detail",
        evidence_category="B",
        source_url="https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation",
        source_id=11040,
        document_id=2235,
        version_id=None,
        exact_supporting_passage=(
            "You use documents to show that you meet the requirements for naturalisation. "
            "Have official foreign documents legalised and translated… Do you not have a "
            "passport or birth certificate? Then you can obtain one…"
        ),
        directly_answers=False,
        pathway_specific=True,
        extraction_complete_enough=False,
        explanation=(
            "Discusses documents conceptually and Dutch PDFs 2249/2250 expand guidance, "
            "but no complete structured checklist comparable to GOV.UK documents page."
        ),
        remediation_category="inspect_existing_document",
        uncertainty="Dutch PDFs may contain fuller lists; language metadata mismatch flagged.",
    )
    add(
        country="NL",
        pathway_id="nl_naturalisation",
        original_pathway_id="nl_naturalisation",
        requirement="application_process",
        previous_classification="COMPLETE",
        proposed_classification="PARTIAL",
        evidence_quality_issue="cross_pathway_or_contaminated_heading",
        evidence_category="A",
        source_url="https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation",
        source_id=11040,
        document_id=2235,
        version_id=None,
        exact_supporting_passage=(
            "FALSE-POSITIVE prior evidence: heading 'These are the steps in the application "
            "process for the residence permit.' Also present: Completing the application form; "
            "Send application form; Apply for a Dutch passport or identity card."
        ),
        directly_answers=False,
        pathway_specific=False,
        extraction_complete_enough=False,
        explanation=(
            "Cited COMPLETE relied on a residence-permit process heading embedded on the "
            "naturalisation page. Naturalisation form steps exist but are incomplete/"
            "contaminated; downgrade to PARTIAL pending human review."
        ),
        remediation_category="correct_evidence_selection",
    )
    add(
        country="NL",
        pathway_id="nl_naturalisation",
        original_pathway_id="nl_naturalisation",
        requirement="financial_or_sponsorship",
        previous_classification="NOT_APPLICABLE",
        proposed_classification="NOT_APPLICABLE",
        evidence_quality_issue="none",
        evidence_category="A",
        source_url="https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation",
        source_id=11040,
        document_id=2235,
        version_id=None,
        exact_supporting_passage="Pathway seed financial_applicable=false for naturalisation.",
        directly_answers=True,
        pathway_specific=True,
        extraction_complete_enough=True,
        explanation="Financial/sponsorship not an applicable critical field for naturalisation pathway.",
        remediation_category="human_verification",
    )
    add(
        country="NL",
        pathway_id="nl_naturalisation",
        original_pathway_id="nl_naturalisation",
        requirement="official_provenance",
        previous_classification="COMPLETE",
        proposed_classification="COMPLETE",
        evidence_quality_issue="language_metadata_mismatch",
        evidence_category="A",
        source_url="https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation",
        source_id=11040,
        document_id=2235,
        version_id=None,
        exact_supporting_passage="IND official naturalisation guidance; PDFs 2249/2250 are Dutch under /en/ URLs.",
        directly_answers=True,
        pathway_specific=True,
        extraction_complete_enough=True,
        explanation="Official IND provenance intact; note doc_language=en vs version_language=nl on PDFs.",
        remediation_category="human_verification",
    )

    # --- NZ citizenship ---
    add(
        country="NZ",
        pathway_id="nz_citizenship",
        original_pathway_id="nz_citizenship",
        requirement="eligibility",
        previous_classification="COMPLETE",
        proposed_classification="PARTIAL",
        evidence_quality_issue="shared_broad_intro",
        evidence_category="B",
        source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/",
        source_id=11032,
        document_id=2194,
        version_id=None,
        exact_supporting_passage=(
            "Step 1 Understand the types of citizenship — There are 3 main types of New Zealand "
            "citizenship. The type you can get depends on your situation."
        ),
        directly_answers=False,
        pathway_specific=True,
        extraction_complete_enough=False,
        explanation=(
            "Eligibility previously shared a broad intro also used for documents. Types overview "
            "is not a full eligibility criteria set → PARTIAL."
        ),
        remediation_category="inspect_existing_document",
    )
    add(
        country="NZ",
        pathway_id="nz_citizenship",
        original_pathway_id="nz_citizenship",
        requirement="required_documents",
        previous_classification="COMPLETE",
        proposed_classification="COMPLETE",
        evidence_quality_issue="shared_broad_intro_resolved",
        evidence_category="A",
        source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/",
        source_id=11032,
        document_id=2195,
        version_id=None,
        exact_supporting_passage=(
            "Get your photo and documents ready… list includes: full birth certificate or birth "
            "record; current/most recent passport or travel document; photo <6 months; identity referee rules."
        ),
        directly_answers=True,
        pathway_specific=True,
        extraction_complete_enough=True,
        explanation="Independent documents list on 'what to get ready' page supports required_documents.",
        remediation_category="human_verification",
    )
    add(
        country="NZ",
        pathway_id="nz_citizenship",
        original_pathway_id="nz_citizenship",
        requirement="application_process",
        previous_classification="COMPLETE",
        proposed_classification="COMPLETE",
        evidence_quality_issue="none",
        evidence_category="A",
        source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/",
        source_id=11032,
        document_id=2194,
        version_id=None,
        exact_supporting_passage="How to apply for NZ citizenship — Follow these steps when you apply for New Zealand citizenship.",
        directly_answers=True,
        pathway_specific=True,
        extraction_complete_enough=True,
        explanation="Dedicated how-to-apply steps page.",
        remediation_category="human_verification",
    )
    add(
        country="NZ",
        pathway_id="nz_citizenship",
        original_pathway_id="nz_citizenship",
        requirement="financial_or_sponsorship",
        previous_classification="NOT_APPLICABLE",
        proposed_classification="NOT_APPLICABLE",
        evidence_quality_issue="none",
        evidence_category="A",
        source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/",
        source_id=11032,
        document_id=2194,
        version_id=None,
        exact_supporting_passage="financial_applicable=false for NZ citizenship pathway seed.",
        directly_answers=True,
        pathway_specific=True,
        extraction_complete_enough=True,
        explanation="Financial/sponsorship not applicable to citizenship grant pathway.",
        remediation_category="human_verification",
    )
    add(
        country="NZ",
        pathway_id="nz_citizenship",
        original_pathway_id="nz_citizenship",
        requirement="official_provenance",
        previous_classification="COMPLETE",
        proposed_classification="COMPLETE",
        evidence_quality_issue="none",
        evidence_category="A",
        source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/",
        source_id=11032,
        document_id=2194,
        version_id=None,
        exact_supporting_passage="Official govt.nz / DIA citizenship guidance via approved source 11032.",
        directly_answers=True,
        pathway_specific=True,
        extraction_complete_enough=True,
        explanation="Official provenance confirmed.",
        remediation_category="human_verification",
    )

    # --- IE employment permits ---
    add(
        country="IE",
        pathway_id="ie_employment_permits",
        original_pathway_id="ie_employment_permits",
        requirement="eligibility",
        previous_classification="COMPLETE",
        proposed_classification="PARTIAL",
        evidence_quality_issue="navigation_heading_only",
        evidence_category="B",
        source_url="https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/",
        source_id=9090,
        document_id=1554,
        version_id=None,
        exact_supporting_passage=(
            "Heading 'Eligibility and requirements' with body only: "
            "'Information for applicants on eligibility and requirements'."
        ),
        directly_answers=False,
        pathway_specific=True,
        extraction_complete_enough=False,
        explanation="Navigation teaser, not substantive eligibility criteria → downgrade COMPLETE to PARTIAL.",
        remediation_category="discover_missing_page",
    )
    add(
        country="IE",
        pathway_id="ie_employment_permits",
        original_pathway_id="ie_employment_permits",
        requirement="required_documents",
        previous_classification="COMPLETE",
        proposed_classification="PARTIAL",
        evidence_quality_issue="navigation_heading_only",
        evidence_category="B",
        source_url="https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/",
        source_id=9090,
        document_id=1555,
        version_id=None,
        exact_supporting_passage="Checklists — Use the appropriate checklist to help with your permit application:",
        directly_answers=False,
        pathway_specific=True,
        extraction_complete_enough=False,
        explanation="Checklist navigation without embedded checklist items in extraction.",
        remediation_category="discover_missing_page",
    )
    add(
        country="IE",
        pathway_id="ie_employment_permits",
        original_pathway_id="ie_employment_permits",
        requirement="application_process",
        previous_classification="COMPLETE",
        proposed_classification="PARTIAL",
        evidence_quality_issue="thin_apply_blurb",
        evidence_category="B",
        source_url="https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/",
        source_id=9090,
        document_id=1555,
        version_id=None,
        exact_supporting_passage=(
            "Apply for a permit — Employment Permits Online allows applicants to apply, "
            "make payments and submit supporting documentation online."
        ),
        directly_answers=False,
        pathway_specific=True,
        extraction_complete_enough=False,
        explanation="Confirms online channel exists but lacks step-by-step process content.",
        remediation_category="inspect_existing_document",
    )
    add(
        country="IE",
        pathway_id="ie_employment_permits",
        original_pathway_id="ie_employment_permits",
        requirement="financial_or_sponsorship",
        previous_classification="COMPLETE",
        proposed_classification="PARTIAL",
        evidence_quality_issue="thin_salary_mention",
        evidence_category="B",
        source_url="https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/",
        source_id=9090,
        document_id=2067,
        version_id=None,
        exact_supporting_passage="Roadmap for gradual increase in employment permit salary thresholds (news/overview).",
        directly_answers=False,
        pathway_specific=True,
        extraction_complete_enough=False,
        explanation="Salary threshold mentions without full remuneration rules for each permit type.",
        remediation_category="discover_missing_page",
    )
    add(
        country="IE",
        pathway_id="ie_employment_permits",
        original_pathway_id="ie_employment_permits",
        requirement="official_provenance",
        previous_classification="COMPLETE",
        proposed_classification="COMPLETE",
        evidence_quality_issue="none",
        evidence_category="A",
        source_url="https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/",
        source_id=9090,
        document_id=1554,
        version_id=None,
        exact_supporting_passage="DETE official employment permits site (approved source 9090).",
        directly_answers=True,
        pathway_specific=True,
        extraction_complete_enough=True,
        explanation="Official provenance OK despite thin requirement pages.",
        remediation_category="human_verification",
    )

    # --- Near-ready ---
    add(
        country="SG",
        pathway_id="sg_employment_pass",
        original_pathway_id="sg_employment_pass",
        requirement="required_documents",
        previous_classification="UNEVALUATED",
        proposed_classification="PARTIAL",
        evidence_quality_issue="hub_tile_without_list",
        evidence_category="B",
        source_url="https://www.mom.gov.sg/passes-and-permits/employment-pass",
        source_id=9078,
        document_id=1538,
        version_id=None,
        exact_supporting_passage="Documents required — Documents required to apply for an Employment Pass.",
        directly_answers=False,
        pathway_specific=True,
        extraction_complete_enough=False,
        explanation=(
            "UNEVALUATED was not MISSING: a Documents required section exists but is a hub "
            "teaser without the actual document list. Assessed → PARTIAL."
        ),
        remediation_category="discover_missing_page",
    )
    add(
        country="SG",
        pathway_id="sg_permanent_resident",
        original_pathway_id="sg_permanent_resident",
        requirement="required_documents",
        previous_classification="UNEVALUATED",
        proposed_classification="PARTIAL",
        evidence_quality_issue="external_pdf_checklist_not_ingested",
        evidence_category="B",
        source_url="https://www.ica.gov.sg/information-and-e-services/citizenship-and-permanent-residence/permanent-residence/permanent-residence-application",
        source_id=9079,
        document_id=1544,
        version_id=None,
        exact_supporting_passage=(
            "Documents Required — Refer to the checklist on the documents required (PDF, 84KB). "
            "You will need to provide original documents and official translations…"
        ),
        directly_answers=False,
        pathway_specific=True,
        extraction_complete_enough=False,
        explanation="Points to an external PDF checklist not in the ingested corpus → PARTIAL.",
        remediation_category="propose_allowlist_expansion",
    )
    add(
        country="CA",
        pathway_id="ca_express_entry",
        original_pathway_id="ca_express_entry",
        requirement="required_documents",
        previous_classification="PARTIAL",
        proposed_classification="PARTIAL",
        evidence_quality_issue="profile_docs_not_full_PR_checklist",
        evidence_category="B",
        source_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry.html",
        source_id=9083,
        document_id=2050,
        version_id=None,
        exact_supporting_passage="Get your documents ready — You may need information from some or all of these documents when you create a profile:",
        directly_answers=False,
        pathway_specific=True,
        extraction_complete_enough=False,
        explanation="Profile-prep docs present; full PR application document checklist still incomplete.",
        remediation_category="inspect_existing_document",
    )
    add(
        country="NL",
        pathway_id="nl_highly_skilled_migrant",
        original_pathway_id="nl_highly_skilled_migrant",
        requirement="required_documents",
        previous_classification="UNEVALUATED",
        proposed_classification="UNEVALUATED",
        evidence_quality_issue="unrelated_passport_mvv_snippet",
        evidence_category="D",
        source_url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant",
        source_id=89,
        document_id=None,
        version_id=None,
        exact_supporting_passage=(
            "Prior hit: 'Your passport must be valid for at least another 6 months when the "
            "Dutch representation places the MVV…' — MVV/passport crumb, not HSM document checklist."
        ),
        directly_answers=False,
        pathway_specific=False,
        extraction_complete_enough=False,
        explanation="Cannot promote UNEVALUATED→MISSING without confirming absence across all linked docs; remains UNEVALUATED.",
        remediation_category="inspect_existing_document",
        uncertainty="Need human scan of HSM pages/forms 7511/7514 for true document lists.",
    )
    add(
        country="NL",
        pathway_id="nl_permanent_residence",
        original_pathway_id="nl_permanent_residence",
        requirement="required_documents",
        previous_classification="MISSING",
        proposed_classification="MISSING",
        evidence_quality_issue="genuine_gap",
        evidence_category="C",
        source_url="https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/",
        source_id=11039,
        document_id=None,
        version_id=None,
        exact_supporting_passage="",
        directly_answers=False,
        pathway_specific=True,
        extraction_complete_enough=False,
        explanation="No relevant documents checklist found in ingested permanent residence extraction.",
        remediation_category="discover_missing_page",
    )
    add(
        country="NL",
        pathway_id="nl_student_residence",
        original_pathway_id="nl_study",
        requirement="financial_or_sponsorship",
        previous_classification="UNEVALUATED",
        proposed_classification="UNEVALUATED",
        evidence_quality_issue="insufficient_assessment_confidence",
        evidence_category="D",
        source_url="https://ind.nl/en/residence-permits/study",
        source_id=11038,
        document_id=None,
        version_id=None,
        exact_supporting_passage="",
        directly_answers=False,
        pathway_specific=True,
        extraction_complete_enough=False,
        explanation="Financial capacity signals not reliably isolated; keep UNEVALUATED (do not coerce to MISSING).",
        remediation_category="inspect_existing_document",
    )
    add(
        country="AU",
        pathway_id="au_student_500",
        original_pathway_id="au_student_500",
        requirement="financial_or_sponsorship",
        previous_classification="UNEVALUATED",
        proposed_classification="PARTIAL",
        evidence_quality_issue="pointer_without_full_funds_table",
        evidence_category="B",
        source_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500",
        source_id=11023,
        document_id=2218,
        version_id=None,
        exact_supporting_passage=(
            "For more information see 'Evidence you have enough money for your stay' section "
            "under Gather documents."
        ),
        directly_answers=False,
        pathway_specific=True,
        extraction_complete_enough=False,
        explanation=(
            "Financial requirement is referenced but full funds evidence section appears "
            "thin/missing in extraction (PageSchema hub limits). Assessed → PARTIAL not MISSING."
        ),
        remediation_category="reprocess_extraction",
    )

    return d
