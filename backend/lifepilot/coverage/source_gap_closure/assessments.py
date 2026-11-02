"""Phase 6F.2 critical-field reassessments (automated_provisional)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FieldAssessment:
    pathway_id: str
    country: str
    requirement: str
    previous_6f1: str
    classification: str
    evidence_repair: str
    document_id: int | None
    version_id: int | None
    source_id: int
    source_url: str
    passage: str
    explanation: str
    remediation_remaining: str
    reviewer_status: str = "automated_provisional"
    not_applicable_rationale: str = ""
    entity_type: str = "pathway"
    conditional_applicability: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "pathway_id": self.pathway_id,
            "country": self.country,
            "requirement": self.requirement,
            "previous_6f1": self.previous_6f1,
            "classification": self.classification,
            "evidence_repair": self.evidence_repair,
            "document_id": self.document_id or "",
            "version_id": self.version_id or "",
            "source_id": self.source_id,
            "source_url": self.source_url,
            "passage": self.passage[:700],
            "explanation": self.explanation,
            "conditional_applicability": self.conditional_applicability,
            "remediation_remaining": self.remediation_remaining,
            "reviewer_status": self.reviewer_status,
            "not_applicable_rationale": self.not_applicable_rationale,
            "entity_type": self.entity_type,
        }


def build_assessments(*, ica_doc_id: int | None = 2252) -> list[FieldAssessment]:
    a: list[FieldAssessment] = []

    def add(**kwargs: Any) -> None:
        a.append(FieldAssessment(**kwargs))

    # -------- GB (retain) --------
    gb = dict(country="GB", pathway_id="gb_skilled_worker", source_id=86, entity_type="pathway")
    for req, did, url, passage, prev in [
        (
            "eligibility",
            160,
            "https://www.gov.uk/skilled-worker-visa/print",
            "To qualify for a Skilled Worker visa, you must: work for an approved UK employer; have a CoS; do an eligible job; be paid a minimum salary.",
            "COMPLETE",
        ),
        (
            "required_documents",
            155,
            "https://www.gov.uk/skilled-worker-visa/documents-you-must-provide",
            "Documents you'll need to apply — CoS reference; proof of English; valid passport; job title and salary; occupation code; employer name and sponsor licence number.",
            "COMPLETE",
        ),
        (
            "application_process",
            160,
            "https://www.gov.uk/skilled-worker-visa/print",
            "How to apply — You must apply online. Outside UK / extend / switch paths listed.",
            "COMPLETE",
        ),
        (
            "financial_or_sponsorship",
            160,
            "https://www.gov.uk/skilled-worker-visa/print",
            "CoS sponsorship required; personal savings evidence unless employer support shown on CoS.",
            "COMPLETE",
        ),
        (
            "official_provenance",
            99,
            "https://www.gov.uk/skilled-worker-visa",
            "Approved GOV.UK Home Office Skilled Worker source 86.",
            "COMPLETE",
        ),
    ]:
        add(
            **gb,
            requirement=req,
            previous_6f1=prev,
            classification="COMPLETE",
            evidence_repair="unchanged",
            document_id=did,
            version_id=None,
            source_url=url,
            passage=passage,
            explanation="Reconfirmed from 6F.1; independent spot-check retained.",
            remediation_remaining="human_verification",
        )

    # -------- NL naturalisation (retain) --------
    nl = dict(country="NL", pathway_id="nl_naturalisation", source_id=11040, entity_type="pathway")
    add(**nl, requirement="eligibility", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2235, version_id=None, source_url="https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation", passage="Requirements for naturalisation: age, identity docs, 5-year residence, residence status, civic integration A2.", explanation="Retained.", remediation_remaining="human_verification")
    add(**nl, requirement="required_documents", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2249, version_id=None, source_url="https://ind.nl/en/forms/5013.pdf", passage="Checklist 2: Welke documenten moet u meenemen? — naturalisatie documenten (paspoort, …).", explanation="Dutch PDF checklist retained.", remediation_remaining="human_verification")
    add(**nl, requirement="application_process", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2249, version_id=None, source_url="https://ind.nl/en/forms/5013.pdf", passage="Wat moet u doen om Nederlander te worden door naturalisatie? — procedure steps.", explanation="Naturalisation procedure retained.", remediation_remaining="human_verification")
    add(**nl, requirement="financial_or_sponsorship", previous_6f1="NOT_APPLICABLE", classification="NOT_APPLICABLE", evidence_repair="unchanged", document_id=2235, version_id=None, source_url="https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation", passage="Naturalisation centres on residence, identity, integration—not employer sponsorship funds.", explanation="N/A with pathway-specific rationale.", remediation_remaining="human_verification", not_applicable_rationale="Citizenship grant procedure; official requirements emphasise residence duration, identity, and civic integration rather than employer sponsorship or visa maintenance funds.")
    add(**nl, requirement="official_provenance", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2235, version_id=None, source_url="https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation", passage="IND official naturalisation pages and forms 5013/6075.", explanation="Official provenance intact.", remediation_remaining="human_verification")

    # -------- NZ grant (still PARTIAL eligibility) --------
    nz = dict(country="NZ", pathway_id="nz_citizenship_by_grant", source_id=11032, entity_type="pathway")
    add(**nz, requirement="eligibility", previous_6f1="PARTIAL", classification="PARTIAL", evidence_repair="unchanged", document_id=2194, version_id=None, source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/", passage="Step 2 Check you meet the requirements — Use the self-check tool to find out if you are eligible… Types: birth, descent and grant.", explanation="Approved how-to-apply subtree still defers to interactive self-check; birth/descent not used as substitutes. Remains PARTIAL.", remediation_remaining="discover_missing_page", conditional_applicability="citizenship_by_grant_only")
    add(**nz, requirement="required_documents", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2195, version_id=None, source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/what-to-get-ready-before-you-apply/", passage="Get your photo and documents ready — birth certificate/record; passport/travel document; photo; identity referee rules.", explanation="Retained.", remediation_remaining="human_verification")
    add(**nz, requirement="application_process", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2194, version_id=None, source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/", passage="How to apply for NZ citizenship — Follow these steps when you apply.", explanation="Retained.", remediation_remaining="human_verification")
    add(**nz, requirement="financial_or_sponsorship", previous_6f1="NOT_APPLICABLE", classification="NOT_APPLICABLE", evidence_repair="unchanged", document_id=2194, version_id=None, source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/", passage="Citizenship by grant focuses on presence/character/identity, not employer sponsorship.", explanation="N/A with rationale.", remediation_remaining="human_verification", not_applicable_rationale="Citizenship by grant is not an employment/sponsorship visa; official materials emphasise identity documents and eligibility self-check, not financial sponsorship thresholds.")
    add(**nz, requirement="official_provenance", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2194, version_id=None, source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/", passage="Official govt.nz / DIA citizenship guidance.", explanation="Retained.", remediation_remaining="human_verification")

    # -------- IE hub + permits --------
    add(
        country="IE",
        pathway_id="ie_employment_permits_hub",
        requirement="eligibility",
        previous_6f1="NOT_APPLICABLE",
        classification="NOT_APPLICABLE",
        evidence_repair="unchanged",
        document_id=1554,
        version_id=None,
        source_id=9090,
        source_url="https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/",
        passage="Employment permits landing aggregates multiple permit types.",
        explanation="Hub retained; not a single legal pathway.",
        remediation_remaining="none",
        entity_type="information_hub",
        not_applicable_rationale="Information hub; assess Critical Skills and General Employment Permit pathways instead.",
    )
    for pid, did, title, suffix in [
        ("ie_critical_skills_employment_permit", 2169, "Critical Skills Employment Permit", "permit-types/critical-skills-employment-permit"),
        ("ie_general_employment_permit", 2168, "General Employment Permit", "permit-types/general-employment-permit"),
    ]:
        base = dict(
            country="IE",
            pathway_id=pid,
            source_id=9090,
            entity_type="pathway",
            document_id=did,
            version_id=None,
            source_url=f"https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/{suffix}",
        )
        add(
            **base,
            requirement="eligibility",
            previous_6f1="COMPLETE",
            classification="COMPLETE",
            evidence_repair="corrected_match",
            passage=(
                f"{title} — Criteria for eligibility: job offer from a bona-fide employer "
                "based and trading in Ireland and registered with Revenue; occupation and "
                "qualification/remuneration rules on the permit page."
            ),
            explanation="Reconfirmed with substantive eligibility paragraph (not heading alone).",
            remediation_remaining="human_verification",
        )
        add(
            **base,
            requirement="financial_or_sponsorship",
            previous_6f1="COMPLETE",
            classification="COMPLETE",
            evidence_repair="corrected_match",
            passage=f"{title} — Remuneration criteria section with permit salary/remuneration rules.",
            explanation="Remuneration criteria remain pathway-specific financial requirements.",
            remediation_remaining="human_verification",
        )
        add(
            **base,
            requirement="application_process",
            previous_6f1="COMPLETE",
            classification="COMPLETE",
            evidence_repair="corrected_match",
            passage=f"{title} — The application process / How to make a … application (Employment Permits Online).",
            explanation="Permit-specific process retained with passage beyond heading.",
            remediation_remaining="human_verification",
        )
        add(
            **base,
            requirement="required_documents",
            previous_6f1="PARTIAL",
            classification="PARTIAL",
            evidence_repair="unchanged",
            passage=(
                "Application-forms page points to 'Checklists to assist with permit applications' "
                "but checklist PDF bodies are hosted under /en/Publications/… outside the "
                "employment-permits allowlist. Contract of employment is required for all "
                "applications; full checklist not ingested."
            ),
            explanation="Exact checklist PDFs discovered from permit pages but out of allowlist; proposal queued.",
            remediation_remaining="propose_allowlist_expansion",
        )
        add(
            **base,
            requirement="official_provenance",
            previous_6f1="COMPLETE",
            classification="COMPLETE",
            evidence_repair="unchanged",
            passage="DETE enterprise.gov.ie employment permits (approved source 9090).",
            explanation="Official provenance retained.",
            remediation_remaining="human_verification",
        )

    # -------- SG Employment Pass (retain ready) --------
    sg = dict(country="SG", pathway_id="sg_employment_pass", source_id=9078, entity_type="pathway")
    add(**sg, requirement="eligibility", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2007, version_id=None, source_url="https://www.mom.gov.sg/passes-and-permits/employment-pass/eligibility", passage="Eligibility for Employment Pass — qualifying salary / COMPASS criteria.", explanation="Retained.", remediation_remaining="human_verification")
    add(**sg, requirement="required_documents", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2031, version_id=None, source_url="https://www.mom.gov.sg/passes-and-permits/employment-pass/documents-required", passage="Documents required — passport particulars; company ACRA profile; qualification verification; regulated professions docs.", explanation="Retained.", remediation_remaining="human_verification")
    add(**sg, requirement="application_process", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=1540, version_id=None, source_url="https://www.mom.gov.sg/passes-and-permits/employment-pass/apply-for-a-pass", passage="Apply for an Employment Pass — how to apply and check status.", explanation="Retained.", remediation_remaining="human_verification")
    add(**sg, requirement="financial_or_sponsorship", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2007, version_id=None, source_url="https://www.mom.gov.sg/passes-and-permits/employment-pass/eligibility", passage="Employment Pass qualifying salary / COMPASS salary benchmarks.", explanation="Retained.", remediation_remaining="human_verification")
    add(**sg, requirement="official_provenance", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=1538, version_id=None, source_url="https://www.mom.gov.sg/passes-and-permits/employment-pass", passage="MOM Employment Pass approved source 9078.", explanation="Retained.", remediation_remaining="human_verification")

    # -------- SG PR (ICA checklist ingested) --------
    sgpr = dict(country="SG", pathway_id="sg_permanent_resident", source_id=9079, entity_type="pathway")
    add(**sgpr, requirement="eligibility", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=1544, version_id=None, source_url="https://www.ica.gov.sg/information-and-e-services/citizenship-and-permanent-residence/permanent-residence/permanent-residence-application", passage="Eligibility — categories with local sponsor table and contribution factors.", explanation="Retained.", remediation_remaining="human_verification")
    add(
        **sgpr,
        requirement="required_documents",
        previous_6f1="PARTIAL",
        classification="COMPLETE" if ica_doc_id else "PARTIAL",
        evidence_repair="newly_ingested" if ica_doc_id else "unchanged",
        document_id=ica_doc_id,
        version_id=None,
        source_url="https://www.ica.gov.sg/docs/ica/ica/ipses/pr-document-checklist.pdf",
        passage=(
            "Document Checklist for Permanent Residence — Before you apply online, prepare "
            "documents where applicable: passport-sized photo; passport/travel document; "
            "birth certificate/household register; marriage certificate; educational "
            "certificates/transcripts; letter of employment; pay slips last 6 months; "
            "income tax assessments (conditional)."
            if ica_doc_id
            else "Checklist PDF not ingested."
        ),
        explanation=(
            "Ingested approved ICA checklist PDF; table rows list concrete document items "
            "for applicant/spouse/sponsor. Pathway not auto-marked ready (financial still open)."
            if ica_doc_id
            else "Awaiting ICA ingest."
        ),
        remediation_remaining="human_verification" if ica_doc_id else "propose_allowlist_expansion",
        conditional_applicability="items marked where applicable / aged-parent additional docs",
    )
    add(**sgpr, requirement="application_process", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=1544, version_id=None, source_url="https://www.ica.gov.sg/information-and-e-services/citizenship-and-permanent-residence/permanent-residence/permanent-residence-application", passage="Application Process / Procedure with and without local sponsor.", explanation="Retained.", remediation_remaining="human_verification")
    add(
        **sgpr,
        requirement="financial_or_sponsorship",
        previous_6f1="PARTIAL",
        classification="PARTIAL",
        evidence_repair="newly_linked",
        document_id=ica_doc_id or 1544,
        version_id=None,
        source_url="https://www.ica.gov.sg/docs/ica/ica/ipses/pr-document-checklist.pdf",
        passage=(
            "Checklist Employment section requires letter of employment stating basic and "
            "gross salary, pay slips (6 months), and income tax assessments when working "
            "overseas; local sponsor categories remain on the application page. Not a "
            "single numeric sponsorship threshold."
        ),
        explanation=(
            "Financial/sponsorship applicability confirmed via sponsor categories + income "
            "documentary evidence on checklist; full threshold rules still incomplete → PARTIAL."
        ),
        remediation_remaining="inspect_existing_document",
    )
    add(**sgpr, requirement="official_provenance", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=1544, version_id=None, source_url="https://www.ica.gov.sg/information-and-e-services/citizenship-and-permanent-residence/permanent-residence/permanent-residence-application", passage="ICA official PR application guidance + approved checklist PDF.", explanation="Official provenance valid.", remediation_remaining="human_verification")

    # -------- CA Express Entry --------
    ca = dict(country="CA", pathway_id="ca_express_entry", source_id=9083, entity_type="pathway")
    add(
        **ca,
        requirement="eligibility",
        previous_6f1="PARTIAL",
        classification="PARTIAL",
        evidence_repair="normalized_split",
        document_id=2013,
        version_id=None,
        source_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry/who-can-apply.html",
        passage=(
            "Express Entry is an online system that we use to manage immigration applications "
            "for 3 programs: Federal Skilled Worker, Canadian Experience Class, Federal "
            "Skilled Trades — key differences in eligibility criteria."
        ),
        explanation=(
            "Pathway is broader than one legal eligibility rule set. Propose normalization "
            "into program pathways rather than marking COMPLETE."
        ),
        remediation_remaining="normalize_pathway",
        conditional_applicability="program-specific (FSW/CEC/FST)",
    )
    add(
        **ca,
        requirement="required_documents",
        previous_6f1="PARTIAL",
        classification="PARTIAL",
        evidence_repair="newly_linked",
        document_id=2050,
        version_id=None,
        source_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry/documents.html",
        passage=(
            "Documents hub distinguishes profile documents from post-invitation permanent "
            "residence application documents (language, ECA, police, job offer, proof of funds)."
        ),
        explanation="Improved linking to docs hub + child pages; still not one universal checklist → PARTIAL.",
        remediation_remaining="normalize_pathway",
        conditional_applicability="profile vs post-ITA packages differ",
    )
    add(**ca, requirement="application_process", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=1542, version_id=None, source_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry.html", passage="Express Entry process overview: profile → invitation → apply for PR.", explanation="High-level process retained.", remediation_remaining="human_verification")
    add(
        **ca,
        requirement="financial_or_sponsorship",
        previous_6f1="PARTIAL",
        classification="COMPLETE",
        evidence_repair="newly_linked",
        document_id=2142,
        version_id=None,
        source_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry/documents/proof-funds.html",
        passage=(
            "Proof of funds — How much money you need table (CAD, updated): 1=$15,263; "
            "2=$19,001; 3=$23,360; … Who needs funds (FSW/FST) vs exemptions (CEC / valid "
            "job offer). Bank letter requirements listed."
        ),
        explanation="Settlement-funds table and exemptions are substantive and already ingested.",
        remediation_remaining="human_verification",
        conditional_applicability="exempt for CEC or authorized workers with valid job offer",
    )
    add(**ca, requirement="official_provenance", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=1542, version_id=None, source_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry.html", passage="IRCC canada.ca Express Entry approved source 9083.", explanation="Retained.", remediation_remaining="human_verification")

    # -------- NL HSM (promote docs via 7511) --------
    hsm = dict(country="NL", pathway_id="nl_highly_skilled_migrant", source_id=89, entity_type="pathway")
    add(**hsm, requirement="eligibility", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=150, version_id=None, source_url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant", passage="Requirements — recognised sponsor employer must apply; extra requirements for researchers/doctors.", explanation="Retained.", remediation_remaining="human_verification")
    add(
        **hsm,
        requirement="required_documents",
        previous_6f1="PARTIAL",
        classification="COMPLETE",
        evidence_repair="newly_linked",
        document_id=334,
        version_id=None,
        source_url="https://ind.nl/en/forms/7511.pdf",
        passage=(
            "Means of evidence and documents — Please enclose the following documents with "
            "this form: copy of highly skilled migrant's passport; completed appendix direct "
            "debit collection; (conditional) copy of valid Schengen residence permit; "
            "antecedents certificate if not signed/declared clear; biometric appendix."
        ),
        explanation="Existing approved form 7511 contains itemised document list with conditionals.",
        remediation_remaining="human_verification",
        conditional_applicability="Schengen permit / antecedents certificate conditional",
    )
    add(**hsm, requirement="application_process", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=150, version_id=None, source_url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant", passage="Process and costs — employer online submission via Business Portal.", explanation="Retained.", remediation_remaining="human_verification")
    add(**hsm, requirement="financial_or_sponsorship", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=150, version_id=None, source_url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant", passage="Only an employer recognised by the IND can apply; salary requirements referenced; form collects gross monthly salary.", explanation="Recognised sponsor + salary retained.", remediation_remaining="human_verification")
    add(**hsm, requirement="official_provenance", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=150, version_id=None, source_url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant", passage="IND HSM approved source 89; forms 7511/7514 already in corpus.", explanation="Retained.", remediation_remaining="human_verification")

    # -------- NL permanent residence --------
    nlpr = dict(country="NL", pathway_id="nl_permanent_residence", source_id=11039, entity_type="pathway")
    add(**nlpr, requirement="eligibility", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2216, version_id=None, source_url="https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/permanent-residence-permit", passage="Requirements for permanent residence; IND also checks EU long-term resident requirements first.", explanation="Retained; EU long-term resident distinguished on same page.", remediation_remaining="human_verification", conditional_applicability="regular PR vs EU long-term resident")
    add(
        **nlpr,
        requirement="required_documents",
        previous_6f1="MISSING",
        classification="MISSING",
        evidence_repair="unchanged",
        document_id=2216,
        version_id=None,
        source_url="https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/permanent-residence-permit",
        passage="Page links written application form /en/forms/6009.pdf; checklist body not in current allowlist/corpus.",
        explanation="Form 6009.pdf discovered from stored HTML but outside allowlist pattern; proposal only.",
        remediation_remaining="propose_allowlist_expansion",
    )
    add(**nlpr, requirement="application_process", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2216, version_id=None, source_url="https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/permanent-residence-permit", passage="Process and costs — DigiD application steps.", explanation="Retained.", remediation_remaining="human_verification")
    add(
        **nlpr,
        requirement="financial_or_sponsorship",
        previous_6f1="UNEVALUATED",
        classification="PARTIAL",
        evidence_repair="newly_linked",
        document_id=2216,
        version_id=None,
        source_url="https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/permanent-residence-permit",
        passage=(
            "Requirements list includes: You meet the income requirements. "
            "Not marked NOT_APPLICABLE — income means are applicable for permanent residence."
        ),
        explanation=(
            "Applicability resolved: income requirements apply. Exact thresholds not isolated "
            "on this page → PARTIAL (not N/A)."
        ),
        remediation_remaining="discover_missing_page",
    )
    add(**nlpr, requirement="official_provenance", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2216, version_id=None, source_url="https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/permanent-residence-permit", passage="IND permanent residence approved source 11039.", explanation="Retained.", remediation_remaining="human_verification")

    # -------- NL student --------
    nlst = dict(country="NL", pathway_id="nl_student_residence", source_id=11038, entity_type="pathway")
    add(**nlst, requirement="eligibility", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2212, version_id=None, source_url="https://ind.nl/en/residence-permits/study/student-residence-permit-for-university-or-higher-professional-education", passage="Requirements: registration proof; accredited programme; recognised educational sponsor; full-time; income requirements; study progress.", explanation="Retained with expanded requirements list.", remediation_remaining="human_verification")
    add(
        **nlst,
        requirement="required_documents",
        previous_6f1="PARTIAL",
        classification="PARTIAL",
        evidence_repair="unchanged",
        document_id=2212,
        version_id=None,
        source_url="https://ind.nl/en/residence-permits/study/student-residence-permit-for-university-or-higher-professional-education",
        passage="Requirements reference registration proof and sponsor institution duties; itemised applicant document checklist still incomplete on page.",
        explanation="Still insufficient for COMPLETE document checklist.",
        remediation_remaining="discover_missing_page",
    )
    add(**nlst, requirement="application_process", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2212, version_id=None, source_url="https://ind.nl/en/residence-permits/study/student-residence-permit-for-university-or-higher-professional-education", passage="Process and costs — educational institution applies as recognised sponsor.", explanation="Retained.", remediation_remaining="human_verification")
    add(
        **nlst,
        requirement="financial_or_sponsorship",
        previous_6f1="UNEVALUATED",
        classification="COMPLETE",
        evidence_repair="newly_linked",
        document_id=2212,
        version_id=None,
        source_url="https://ind.nl/en/residence-permits/study/student-residence-permit-for-university-or-higher-professional-education",
        passage=(
            "Requirements: Your educational institution is a sponsor recognised by the IND; "
            "You meet the income requirements. Institution has sponsor obligations (information, "
            "records, duty of care)."
        ),
        explanation="Institutional sponsorship + income requirements evidenced on pathway page.",
        remediation_remaining="human_verification",
        conditional_applicability="income amount details may live on linked income-requirements page",
    )
    add(**nlst, requirement="official_provenance", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2211, version_id=None, source_url="https://ind.nl/en/residence-permits/study", passage="IND study residence approved source 11038.", explanation="Retained.", remediation_remaining="human_verification")

    # -------- AU Student 500 --------
    au = dict(country="AU", pathway_id="au_student_500", source_id=11023, entity_type="pathway")
    add(**au, requirement="eligibility", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2218, version_id=None, source_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500", passage="Enrolment / Genuine Student requirements; health and character.", explanation="Retained; GTE/Genuine Student provenance discrepancy preserved for later metadata work.", remediation_remaining="human_verification")
    add(
        **au,
        requirement="required_documents",
        previous_6f1="PARTIAL",
        classification="PARTIAL",
        evidence_repair="unchanged",
        document_id=2218,
        version_id=None,
        source_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500",
        passage=(
            "Page repeatedly refers to the document checklist / ImmiAccount attachments and "
            "lists some evidence types (financial capacity evidence examples), but does not "
            "present a complete Student 500 document checklist body."
        ),
        explanation="Financial section is rich; universal document checklist still incomplete → PARTIAL.",
        remediation_remaining="discover_missing_page",
    )
    add(**au, requirement="application_process", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2218, version_id=None, source_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500", passage="How to apply / ImmiAccount application guidance.", explanation="Retained.", remediation_remaining="human_verification")
    add(
        **au,
        requirement="financial_or_sponsorship",
        previous_6f1="PARTIAL",
        classification="COMPLETE",
        evidence_repair="newly_linked",
        document_id=2218,
        version_id=None,
        source_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500",
        passage=(
            "Evidence you have enough money — deposits/loans/scholarships; alternatively "
            "parents/partner annual income at least AUD87,856. Work out money needed: travel, "
            "12 months course fees, living costs (students AUD29,710; partner AUD10,394; "
            "child AUD4,449), schooling AUD13,502; travel cost guides; pro-rata formula."
        ),
        explanation="Substantive thresholds and formulas already present in doc 2218 extraction (not keyword-only).",
        remediation_remaining="human_verification",
        conditional_applicability="subsequent entrants / DFAT / Defence / AASES alternatives",
    )
    add(**au, requirement="official_provenance", previous_6f1="COMPLETE", classification="COMPLETE", evidence_repair="unchanged", document_id=2218, version_id=None, source_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500", passage="Home Affairs Student 500 approved source 11023; GTE canonical vs Genuine Student source_url divergence noted.", explanation="Provenance valid; metadata discrepancy preserved.", remediation_remaining="human_verification")

    return a


def pathway_ready(assessments: list[FieldAssessment]) -> bool:
    applicable = [
        a
        for a in assessments
        if a.entity_type == "pathway" and a.classification != "NOT_APPLICABLE"
    ]
    if not applicable:
        return False
    return all(a.classification == "COMPLETE" for a in applicable)
