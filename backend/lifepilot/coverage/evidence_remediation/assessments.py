"""Independent critical-field reassessments for Phase 6F.1 (automated_provisional)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FieldAssessment:
    pathway_id: str
    country: str
    requirement: str
    previous_6f0: str
    classification: str
    evidence_repair: str  # corrected_match|extraction_improved|newly_linked|unchanged|downgraded|normalized_split
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

    def to_row(self) -> dict[str, Any]:
        return {
            "pathway_id": self.pathway_id,
            "country": self.country,
            "requirement": self.requirement,
            "previous_6f0": self.previous_6f0,
            "classification": self.classification,
            "evidence_repair": self.evidence_repair,
            "document_id": self.document_id or "",
            "version_id": self.version_id or "",
            "source_id": self.source_id,
            "source_url": self.source_url,
            "passage": self.passage[:600],
            "explanation": self.explanation,
            "remediation_remaining": self.remediation_remaining,
            "reviewer_status": self.reviewer_status,
            "not_applicable_rationale": self.not_applicable_rationale,
            "entity_type": self.entity_type,
        }


def build_assessments(*, au_financial_improved: bool = False) -> list[FieldAssessment]:
    a: list[FieldAssessment] = []

    def add(**kwargs: Any) -> None:
        a.append(FieldAssessment(**kwargs))

    # -------- GB Skilled Worker (retain provisional ready; corrected passages) --------
    gb = dict(country="GB", pathway_id="gb_skilled_worker", source_id=86, entity_type="pathway")
    add(
        **gb,
        requirement="eligibility",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="corrected_match",
        document_id=160,
        version_id=None,
        source_url="https://www.gov.uk/skilled-worker-visa/print",
        passage=(
            "Eligibility — To qualify for a Skilled Worker visa, you must: work for an "
            "approved UK employer; have a certificate of sponsorship; do an eligible job; "
            "be paid a minimum salary."
        ),
        explanation="Reconfirmed substantive eligibility list on official Skilled Worker guidance.",
        remediation_remaining="human_verification",
    )
    add(
        **gb,
        requirement="required_documents",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="corrected_match",
        document_id=155,
        version_id=None,
        source_url="https://www.gov.uk/skilled-worker-visa/documents-you-must-provide",
        passage=(
            "Documents you'll need to apply — When you apply you’ll need to provide: CoS "
            "reference number; proof of English; valid passport; job title and salary; "
            "occupation code; employer name and sponsor licence number."
        ),
        explanation="Points to documents checklist page, not application timing.",
        remediation_remaining="human_verification",
    )
    add(
        **gb,
        requirement="application_process",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=160,
        version_id=None,
        source_url="https://www.gov.uk/skilled-worker-visa/print",
        passage="How to apply — You must apply online. Outside UK / extend / switch paths listed.",
        explanation="Application process steps present.",
        remediation_remaining="human_verification",
    )
    add(
        **gb,
        requirement="financial_or_sponsorship",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="corrected_match",
        document_id=160,
        version_id=None,
        source_url="https://www.gov.uk/skilled-worker-visa/print",
        passage=(
            "CoS sponsorship required; costs section requires personal savings evidence "
            "unless employer support shown on CoS; documents page lists bank statements."
        ),
        explanation="Sponsorship and funds supported by substantive passages across linked pages.",
        remediation_remaining="human_verification",
    )
    add(
        **gb,
        requirement="official_provenance",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=99,
        version_id=None,
        source_url="https://www.gov.uk/skilled-worker-visa",
        passage="Approved GOV.UK Home Office Skilled Worker source 86.",
        explanation="Official provenance valid.",
        remediation_remaining="human_verification",
    )

    # -------- NL Naturalisation (promote via Dutch PDF checklist + procedure) --------
    nl = dict(country="NL", pathway_id="nl_naturalisation", source_id=11040, entity_type="pathway")
    add(
        **nl,
        requirement="eligibility",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2235,
        version_id=None,
        source_url="https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation",
        passage="Requirements for naturalisation: age, identity docs, 5-year residence, residence status, civic integration A2.",
        explanation="English IND page retains substantive naturalisation eligibility.",
        remediation_remaining="human_verification",
    )
    add(
        **nl,
        requirement="required_documents",
        previous_6f0="PARTIAL",
        classification="COMPLETE",
        evidence_repair="newly_linked",
        document_id=2249,
        version_id=None,
        source_url="https://ind.nl/en/forms/5013.pdf",
        passage=(
            "Checklist 2: Welke documenten moet u meenemen? — Om uw verzoek om naturalisatie "
            "te beoordelen zijn de volgende documenten nodig: geldig paspoort; … "
            "(Dutch content language on version; PDF title 'Hoe kunt u Nederlander worden?')."
        ),
        explanation=(
            "Dutch PDF 2249 contains explicit naturalisation document checklist. "
            "Used version language nl, not URL /en/ assumption."
        ),
        remediation_remaining="human_verification",
    )
    add(
        **nl,
        requirement="application_process",
        previous_6f0="PARTIAL",
        classification="COMPLETE",
        evidence_repair="newly_linked",
        document_id=2249,
        version_id=None,
        source_url="https://ind.nl/en/forms/5013.pdf",
        passage=(
            "5. Wat moet u doen om Nederlander te worden door naturalisatie? — "
            "Informatie inwinnen; Documenten verzamelen; … (naturalisation procedure, "
            "not residence-permit steps)."
        ),
        explanation=(
            "Replaced contaminated residence-permit heading with Dutch naturalisation "
            "procedure section in PDF 2249."
        ),
        remediation_remaining="human_verification",
    )
    add(
        **nl,
        requirement="financial_or_sponsorship",
        previous_6f0="NOT_APPLICABLE",
        classification="NOT_APPLICABLE",
        evidence_repair="unchanged",
        document_id=2235,
        version_id=None,
        source_url="https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation",
        passage="Naturalisation requirements emphasise residence, identity, and integration—not employer sponsorship funds.",
        explanation="N/A retained with pathway-specific rationale (not seed-only).",
        remediation_remaining="human_verification",
        not_applicable_rationale=(
            "Dutch naturalisation is a citizenship grant procedure; official requirements "
            "centre on residence duration, identity documents, and civic integration, not "
            "employer sponsorship or applicant maintenance funds for a visa."
        ),
    )
    add(
        **nl,
        requirement="official_provenance",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2235,
        version_id=None,
        source_url="https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation",
        passage="IND official naturalisation pages and forms 5013/6075.",
        explanation="Official provenance intact; language metadata mismatch remains noted.",
        remediation_remaining="human_verification",
    )

    # -------- NZ citizenship by grant --------
    nz = dict(
        country="NZ",
        pathway_id="nz_citizenship_by_grant",
        source_id=11032,
        entity_type="pathway",
    )
    add(
        **nz,
        requirement="eligibility",
        previous_6f0="PARTIAL",
        classification="PARTIAL",
        evidence_repair="normalized_split",
        document_id=2194,
        version_id=None,
        source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/",
        passage=(
            "Types of citizenship: birth, descent and grant. Step 2 Check you meet the "
            "requirements — Use the self-check tool to find out if you are eligible…"
        ),
        explanation=(
            "Scoped to citizenship by grant. Existing extraction is thin and defers to an "
            "external self-check tool; birth/descent evidence not used. Remains PARTIAL."
        ),
        remediation_remaining="discover_missing_page",
    )
    add(
        **nz,
        requirement="required_documents",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2195,
        version_id=None,
        source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/",
        passage="Get your photo and documents ready — birth certificate/record; passport/travel document; photo; identity referee rules.",
        explanation="Independent documents list retained for grant application prep.",
        remediation_remaining="human_verification",
    )
    add(
        **nz,
        requirement="application_process",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2194,
        version_id=None,
        source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/",
        passage="How to apply for NZ citizenship — Follow these steps when you apply.",
        explanation="How-to-apply steps present.",
        remediation_remaining="human_verification",
    )
    add(
        **nz,
        requirement="financial_or_sponsorship",
        previous_6f0="NOT_APPLICABLE",
        classification="NOT_APPLICABLE",
        evidence_repair="unchanged",
        document_id=2194,
        version_id=None,
        source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/",
        passage="Citizenship by grant requirements focus on presence/character/identity, not employer sponsorship.",
        explanation="N/A with pathway-specific rationale.",
        remediation_remaining="human_verification",
        not_applicable_rationale=(
            "Citizenship by grant is not an employment/sponsorship visa; official how-to-apply "
            "materials emphasise identity documents and eligibility self-check, not financial "
            "sponsorship thresholds."
        ),
    )
    add(
        **nz,
        requirement="official_provenance",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2194,
        version_id=None,
        source_url="https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/how-to-apply-for-nz-citizenship/",
        passage="Official govt.nz / DIA citizenship guidance.",
        explanation="Official provenance valid.",
        remediation_remaining="human_verification",
    )

    # -------- IE: hub demotion + CSEP / GEP pathways --------
    add(
        country="IE",
        pathway_id="ie_employment_permits_hub",
        requirement="eligibility",
        previous_6f0="PARTIAL",
        classification="NOT_APPLICABLE",
        evidence_repair="normalized_split",
        document_id=1554,
        version_id=None,
        source_id=9090,
        source_url="https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/",
        passage="Employment permits landing aggregates multiple permit types.",
        explanation="Hub is not a single legal pathway; requirements vary by permit type.",
        remediation_remaining="none",
        entity_type="information_hub",
        not_applicable_rationale="Information hub; assess Critical Skills and General Employment Permit pathways instead.",
    )
    for pid, did, title, url_suffix in [
        (
            "ie_critical_skills_employment_permit",
            2169,
            "Critical Skills Employment Permit",
            "permit-types/critical-skills-employment-permit",
        ),
        (
            "ie_general_employment_permit",
            2168,
            "General Employment Permit",
            "permit-types/general-employment-permit",
        ),
    ]:
        base = dict(
            country="IE",
            pathway_id=pid,
            source_id=9090,
            entity_type="pathway",
            document_id=did,
            version_id=None,
            source_url=(
                "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/"
                f"employment-permits/{url_suffix}"
            ),
        )
        add(
            **base,
            requirement="eligibility",
            previous_6f0="PARTIAL",
            classification="COMPLETE",
            evidence_repair="newly_linked",
            passage=f"{title} — Criteria for eligibility (employer/employee criteria present on permit page).",
            explanation=f"Permit-specific eligibility criteria on already-ingested doc {did}.",
            remediation_remaining="human_verification",
        )
        add(
            **base,
            requirement="financial_or_sponsorship",
            previous_6f0="PARTIAL",
            classification="COMPLETE",
            evidence_repair="newly_linked",
            passage=f"{title} — Remuneration criteria section with permit salary/remuneration rules.",
            explanation="Remuneration criteria are pathway-specific financial requirements for the permit.",
            remediation_remaining="human_verification",
        )
        add(
            **base,
            requirement="application_process",
            previous_6f0="PARTIAL",
            classification="COMPLETE",
            evidence_repair="newly_linked",
            passage=f"{title} — The application process / How to make a … application.",
            explanation="Permit-specific application process content present.",
            remediation_remaining="human_verification",
        )
        add(
            **base,
            requirement="required_documents",
            previous_6f0="PARTIAL",
            classification="PARTIAL",
            evidence_repair="newly_linked",
            passage="Checklist mentions exist but full supporting-document lists remain thin on these pages.",
            explanation="Eligibility/process/remuneration strong; document checklist still incomplete.",
            remediation_remaining="discover_missing_page",
        )
        add(
            **base,
            requirement="official_provenance",
            previous_6f0="COMPLETE",
            classification="COMPLETE",
            evidence_repair="newly_linked",
            passage="DETE enterprise.gov.ie employment permits (approved source 9090).",
            explanation="Official provenance for permit-type pages.",
            remediation_remaining="human_verification",
        )

    # -------- Near-ready deep validation --------
    # SG Employment Pass
    sg = dict(country="SG", pathway_id="sg_employment_pass", source_id=9078, entity_type="pathway")
    add(
        **sg,
        requirement="eligibility",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="newly_linked",
        document_id=2007,
        version_id=None,
        source_url="https://www.mom.gov.sg/passes-and-permits/employment-pass/eligibility",
        passage="Eligibility for Employment Pass — qualifying salary / COMPASS criteria pages.",
        explanation="Substantive eligibility page already ingested (doc 2007), not hub teaser alone.",
        remediation_remaining="human_verification",
    )
    add(
        **sg,
        requirement="required_documents",
        previous_6f0="PARTIAL",
        classification="COMPLETE",
        evidence_repair="newly_linked",
        document_id=2031,
        version_id=None,
        source_url="https://www.mom.gov.sg/passes-and-permits/employment-pass/documents-required",
        passage=(
            "Documents required — Personal particulars page of candidate’s passport; "
            "company ACRA profile; verification proof for qualifications; additional "
            "documents for regulated professions."
        ),
        explanation="Already-ingested documents-required page contains actual checklist items.",
        remediation_remaining="human_verification",
    )
    add(
        **sg,
        requirement="application_process",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="newly_linked",
        document_id=1540,
        version_id=None,
        source_url="https://www.mom.gov.sg/passes-and-permits/employment-pass/apply-for-a-pass",
        passage="Apply for an Employment Pass — how to apply and check status.",
        explanation="Dedicated apply page with process content.",
        remediation_remaining="human_verification",
    )
    add(
        **sg,
        requirement="financial_or_sponsorship",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="newly_linked",
        document_id=2007,
        version_id=None,
        source_url="https://www.mom.gov.sg/passes-and-permits/employment-pass/eligibility",
        passage="Employment Pass qualifying salary / COMPASS salary benchmarks (employer-paid salary thresholds).",
        explanation="Salary threshold evidence is the applicable financial/sponsorship dimension for EP.",
        remediation_remaining="human_verification",
    )
    add(
        **sg,
        requirement="official_provenance",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=1538,
        version_id=None,
        source_url="https://www.mom.gov.sg/passes-and-permits/employment-pass",
        passage="MOM Employment Pass approved source 9078.",
        explanation="Official provenance valid.",
        remediation_remaining="human_verification",
    )

    # SG PR
    sgpr = dict(
        country="SG",
        pathway_id="sg_permanent_resident",
        source_id=9079,
        entity_type="pathway",
    )
    add(
        **sgpr,
        requirement="eligibility",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=1544,
        version_id=None,
        source_url="https://www.ica.gov.sg/information-and-e-services/citizenship-and-permanent-residence/permanent-residence/permanent-residence-application",
        passage="Eligibility — categories with local sponsor table and contribution factors.",
        explanation="Eligibility content is substantive on PR application page.",
        remediation_remaining="human_verification",
    )
    add(
        **sgpr,
        requirement="required_documents",
        previous_6f0="PARTIAL",
        classification="PARTIAL",
        evidence_repair="unchanged",
        document_id=1544,
        version_id=None,
        source_url="https://www.ica.gov.sg/information-and-e-services/citizenship-and-permanent-residence/permanent-residence/permanent-residence-application",
        passage="Documents Required — Refer to the checklist on the documents required (PDF, 84KB).",
        explanation="Checklist PDF referenced but not present in ingested corpus.",
        remediation_remaining="propose_allowlist_expansion",
    )
    add(
        **sgpr,
        requirement="application_process",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=1544,
        version_id=None,
        source_url="https://www.ica.gov.sg/information-and-e-services/citizenship-and-permanent-residence/permanent-residence/permanent-residence-application",
        passage="Application Process / Procedure with and without local sponsor.",
        explanation="Procedure sections present.",
        remediation_remaining="human_verification",
    )
    add(
        **sgpr,
        requirement="financial_or_sponsorship",
        previous_6f0="NOT_APPLICABLE",
        classification="PARTIAL",
        evidence_repair="downgraded",
        document_id=1544,
        version_id=None,
        source_url="https://www.ica.gov.sg/information-and-e-services/citizenship-and-permanent-residence/permanent-residence/permanent-residence-application",
        passage="Eligibility mentions local sponsor categories; economic contributions considered.",
        explanation=(
            "Seed N/A overturned: PR eligibility explicitly discusses local sponsor and "
            "economic contribution factors. Full financial evidential rules still incomplete."
        ),
        remediation_remaining="inspect_existing_document",
        not_applicable_rationale="",
    )
    add(
        **sgpr,
        requirement="official_provenance",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=1544,
        version_id=None,
        source_url="https://www.ica.gov.sg/information-and-e-services/citizenship-and-permanent-residence/permanent-residence/permanent-residence-application",
        passage="ICA official PR application guidance.",
        explanation="Official provenance valid.",
        remediation_remaining="human_verification",
    )

    # CA Express Entry
    ca = dict(country="CA", pathway_id="ca_express_entry", source_id=9083, entity_type="pathway")
    add(
        **ca,
        requirement="eligibility",
        previous_6f0="COMPLETE",
        classification="PARTIAL",
        evidence_repair="downgraded",
        document_id=1543,
        version_id=None,
        source_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry.html",
        passage="Generic eligibility/admissibility snippets and program overview.",
        explanation="Prior COMPLETE over-relied on generic wording; program-specific eligibility still incomplete in inspected passages.",
        remediation_remaining="inspect_existing_document",
    )
    add(
        **ca,
        requirement="required_documents",
        previous_6f0="PARTIAL",
        classification="PARTIAL",
        evidence_repair="unchanged",
        document_id=2050,
        version_id=None,
        source_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry/documents.html",
        passage="Get your documents ready when you create a profile (profile docs ≠ ITA PR package).",
        explanation="Distinguished profile documents from post-ITA PR checklist; remains PARTIAL.",
        remediation_remaining="discover_missing_page",
    )
    add(
        **ca,
        requirement="application_process",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=1543,
        version_id=None,
        source_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry.html",
        passage="Express Entry process overview: profile → invitation → apply for PR.",
        explanation="High-level process is pathway-specific and substantive enough for provisional COMPLETE.",
        remediation_remaining="human_verification",
    )
    add(
        **ca,
        requirement="financial_or_sponsorship",
        previous_6f0="COMPLETE",
        classification="PARTIAL",
        evidence_repair="downgraded",
        document_id=2051,
        version_id=None,
        source_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry.html",
        passage="Proof of funds update reminder without full settlement-funds table in inspected extraction.",
        explanation="Funds concept present; amounts/table incomplete in current extraction.",
        remediation_remaining="inspect_existing_document",
    )
    add(
        **ca,
        requirement="official_provenance",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=1543,
        version_id=None,
        source_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry.html",
        passage="IRCC canada.ca Express Entry approved source 9083.",
        explanation="Official provenance valid.",
        remediation_remaining="human_verification",
    )

    # NL HSM
    hsm = dict(
        country="NL",
        pathway_id="nl_highly_skilled_migrant",
        source_id=89,
        entity_type="pathway",
    )
    add(
        **hsm,
        requirement="eligibility",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=150,
        version_id=None,
        source_url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant",
        passage="Requirements — recognised sponsor employer must apply; extra requirements for researchers/doctors.",
        explanation="HSM requirements section is substantive.",
        remediation_remaining="human_verification",
    )
    add(
        **hsm,
        requirement="required_documents",
        previous_6f0="UNEVALUATED",
        classification="PARTIAL",
        evidence_repair="newly_linked",
        document_id=150,
        version_id=None,
        source_url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant",
        passage="Application forms / Written application forms; Valid passport required; checklist mentions—not a full itemised document list.",
        explanation="Moved UNEVALUATED→PARTIAL after inspecting HSM page/forms; MVV passport crumb alone rejected.",
        remediation_remaining="inspect_existing_document",
    )
    add(
        **hsm,
        requirement="application_process",
        previous_6f0="PARTIAL",
        classification="COMPLETE",
        evidence_repair="newly_linked",
        document_id=150,
        version_id=None,
        source_url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant",
        passage="Process and costs — steps for residence permit application; employer online submission via Business Portal.",
        explanation="Process steps are pathway-specific for HSM employer-led application.",
        remediation_remaining="human_verification",
    )
    add(
        **hsm,
        requirement="financial_or_sponsorship",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=150,
        version_id=None,
        source_url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant",
        passage="Only an employer recognised by the IND can apply; salary requirements referenced on HSM page.",
        explanation="Recognised sponsor + salary requirements are the financial/sponsorship core for HSM.",
        remediation_remaining="human_verification",
    )
    add(
        **hsm,
        requirement="official_provenance",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=150,
        version_id=None,
        source_url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant",
        passage="IND HSM approved source 89.",
        explanation="Official provenance valid.",
        remediation_remaining="human_verification",
    )

    # NL permanent residence
    nlpr = dict(
        country="NL",
        pathway_id="nl_permanent_residence",
        source_id=11039,
        entity_type="pathway",
    )
    add(
        **nlpr,
        requirement="eligibility",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2216,
        version_id=None,
        source_url="https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/permanent-residence-permit",
        passage="Requirements for permanent residence / EU long-term resident.",
        explanation="Requirements section present.",
        remediation_remaining="human_verification",
    )
    add(
        **nlpr,
        requirement="required_documents",
        previous_6f0="MISSING",
        classification="MISSING",
        evidence_repair="unchanged",
        document_id=2216,
        version_id=None,
        source_url="https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/permanent-residence-permit",
        passage="Application forms links present; no itemised supporting-document checklist in extraction.",
        explanation="Genuine missing checklist after inspecting permanent residence page.",
        remediation_remaining="discover_missing_page",
    )
    add(
        **nlpr,
        requirement="application_process",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2216,
        version_id=None,
        source_url="https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/permanent-residence-permit",
        passage="Process and costs — steps for applying for the residence permit; online DigiD application.",
        explanation="Application process steps present.",
        remediation_remaining="human_verification",
    )
    add(
        **nlpr,
        requirement="financial_or_sponsorship",
        previous_6f0="NOT_APPLICABLE",
        classification="UNEVALUATED",
        evidence_repair="downgraded",
        document_id=2216,
        version_id=None,
        source_url="https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/permanent-residence-permit",
        passage="Requirements page may include income conditions; not confidently isolated in this reassessment.",
        explanation="Seed N/A insufficient; income means may apply—left UNEVALUATED pending human review.",
        remediation_remaining="human_verification",
    )
    add(
        **nlpr,
        requirement="official_provenance",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2216,
        version_id=None,
        source_url="https://ind.nl/en/replace-extend-renew-and-change/permanent-residency/permanent-residence-permit",
        passage="IND permanent residence approved source 11039.",
        explanation="Official provenance valid.",
        remediation_remaining="human_verification",
    )

    # NL student
    nlst = dict(
        country="NL",
        pathway_id="nl_student_residence",
        source_id=11038,
        entity_type="pathway",
    )
    add(
        **nlst,
        requirement="eligibility",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2212,
        version_id=None,
        source_url=(
            "https://ind.nl/en/residence-permits/study/"
            "student-residence-permit-for-university-or-higher-professional-education"
        ),
        passage="Study residence permit requirements on IND university/HBO student page.",
        explanation="Eligibility retained after spot-check of study pathway pages.",
        remediation_remaining="human_verification",
    )
    add(
        **nlst,
        requirement="required_documents",
        previous_6f0="PARTIAL",
        classification="PARTIAL",
        evidence_repair="unchanged",
        document_id=2212,
        version_id=None,
        source_url=(
            "https://ind.nl/en/residence-permits/study/"
            "student-residence-permit-for-university-or-higher-professional-education"
        ),
        passage="Passport/MVV style crumbs without full student document checklist.",
        explanation="Still insufficient for COMPLETE.",
        remediation_remaining="inspect_existing_document",
    )
    add(
        **nlst,
        requirement="application_process",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2212,
        version_id=None,
        source_url=(
            "https://ind.nl/en/residence-permits/study/"
            "student-residence-permit-for-university-or-higher-professional-education"
        ),
        passage="Steps in the application process for student residence permit.",
        explanation="Process steps pathway-specific.",
        remediation_remaining="human_verification",
    )
    add(
        **nlst,
        requirement="financial_or_sponsorship",
        previous_6f0="UNEVALUATED",
        classification="UNEVALUATED",
        evidence_repair="unchanged",
        document_id=2212,
        version_id=None,
        source_url=(
            "https://ind.nl/en/residence-permits/study/"
            "student-residence-permit-for-university-or-higher-professional-education"
        ),
        passage="",
        explanation="Financial capacity not reliably isolated; keep UNEVALUATED (not coerced to MISSING).",
        remediation_remaining="inspect_existing_document",
    )
    add(
        **nlst,
        requirement="official_provenance",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2211,
        version_id=None,
        source_url="https://ind.nl/en/residence-permits/study",
        passage="IND study residence approved source 11038.",
        explanation="Official provenance valid.",
        remediation_remaining="human_verification",
    )

    # AU Student 500
    fin_class = "PARTIAL"
    fin_repair = "extraction_improved" if au_financial_improved else "unchanged"
    fin_passage = (
        "Evidence you have enough money for your stay / cost of living guidance recovered "
        "from PageSchema embedded HTML after extractor merge."
        if au_financial_improved
        else "Pointer to 'Evidence you have enough money for your stay' under Gather documents."
    )
    fin_expl = (
        "Re-extracted stored raw HTML with improved PageSchema merge; financial section "
        "present but still may lack full funds table → PARTIAL."
        if au_financial_improved
        else "Financial section exists in raw PageSchema JSON; awaiting successful re-extract promotion."
    )
    au = dict(country="AU", pathway_id="au_student_500", source_id=11023, entity_type="pathway")
    add(
        **au,
        requirement="eligibility",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2218,
        version_id=None,
        source_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500",
        passage="Enrolment / Genuine Student requirements on Student 500 pages.",
        explanation="Eligibility retained; do not substitute outdated GTE-only framing.",
        remediation_remaining="human_verification",
    )
    add(
        **au,
        requirement="required_documents",
        previous_6f0="COMPLETE",
        classification="PARTIAL",
        evidence_repair="downgraded",
        document_id=2240,
        version_id=None,
        source_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500/disclaimer-for-document-checklist",
        passage="Disclaimer for document checklist / costs-oriented passages are not a checklist.",
        explanation="Prior COMPLETE used cost/timing-like content; checklist still incomplete.",
        remediation_remaining="discover_missing_page",
    )
    add(
        **au,
        requirement="application_process",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2218,
        version_id=None,
        source_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500",
        passage="How to apply / ImmiAccount application guidance.",
        explanation="Application process retained.",
        remediation_remaining="human_verification",
    )
    add(
        **au,
        requirement="financial_or_sponsorship",
        previous_6f0="PARTIAL",
        classification=fin_class,
        evidence_repair=fin_repair,
        document_id=2218,
        version_id=None,
        source_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500",
        passage=fin_passage,
        explanation=fin_expl,
        remediation_remaining="inspect_existing_document",
    )
    add(
        **au,
        requirement="official_provenance",
        previous_6f0="COMPLETE",
        classification="COMPLETE",
        evidence_repair="unchanged",
        document_id=2218,
        version_id=None,
        source_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500",
        passage="Home Affairs Student 500 approved source 11023; doc 2239 shows GTE canonical vs Genuine Student source_url divergence.",
        explanation="Provenance valid; redirect observation noted without inventing final_url.",
        remediation_remaining="human_verification",
    )

    return a


def pathway_ready(assessments: list[FieldAssessment]) -> bool:
    if not assessments:
        return False
    if any(a.entity_type != "pathway" for a in assessments):
        # allow filtering by caller
        pass
    applicable = [
        a
        for a in assessments
        if a.entity_type == "pathway" and a.classification != "NOT_APPLICABLE"
    ]
    if not applicable:
        return False
    return all(a.classification == "COMPLETE" for a in applicable)
