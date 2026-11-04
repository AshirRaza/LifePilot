"""Pathway-aware document applicability for evidence assessment.

A keyword hit alone must never upgrade a requirement. Documents must first
apply to the pathway via URL tokens, official-URL scope, or explicit
title/pathway markers — and must not match hard exclusions for other
immigration categories.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Reuse / extend the 6H URL token map. Tokens match host+path (query stripped).
PATHWAY_URL_TOKENS: dict[str, tuple[str, ...]] = {
    "gb_skilled_worker": ("gov.uk/skilled-worker-visa",),
    "gb_student": ("gov.uk/student-visa",),
    "gb_graduate": ("gov.uk/graduate-visa",),
    "gb_global_talent": ("gov.uk/global-talent",),
    "gb_innovator_founder": ("gov.uk/innovator-founder-visa",),
    "gb_visitor": ("gov.uk/standard-visitor",),
    "gb_citizenship": ("gov.uk/british-citizenship", "gov.uk/becoming-a-british-citizen"),
    "gb_settlement_ilr": (
        "gov.uk/indefinite-leave-to-remain",
        "gov.uk/settle-in-the-uk",
    ),
    "jp_student": (
        "applications/status/student",
        "isa/applications/status/student",
    ),
    "de_eu_blue_card": (
        "blauekarteeu",
        "blaue-karte",
        "blue-card-eu",
        "eu-blue-card",
        "make-it-in-germany.com/en/visa-residence/types/eu-blue-card",
    ),
    "nl_student_residence": ("residence-permits/study/student-residence-permit",),
    "nl_highly_skilled_migrant": ("highly-skilled-migrant", "forms/7511", "forms/7514"),
    "nl_naturalisation": ("through-naturalisation", "forms/5013", "forms/6075"),
    "nl_permanent_residence": ("permanent-residence-permit", "forms/6009"),
    "sg_employment_pass": ("mom.gov.sg/passes-and-permits/employment-pass",),
    "sg_permanent_residence": (
        "permanent-residence-application",
        "pr-document-checklist",
        "completion-of-formalities-for-permanent-residence",
        "citizenship-and-permanent-resi",
    ),
    "sg_permanent_resident": (
        "permanent-residence-application",
        "pr-document-checklist",
        "completion-of-formalities-for-permanent-residence",
        "citizenship-and-permanent-resi",
    ),
    "us_h1b": ("h-1b-specialty-occupations", "h-1b-cap", "/h-1b"),
    "us_l1": ("l-1a-intracompany", "l-1b-intracompany", "/l-1"),
    "us_o1": ("o-1-visa-individuals", "/o-1"),
    "us_e2": ("e-2-treaty-investors", "/e-2"),
    "us_f1": (
        "students-and-exchange-visitors/students-and-employment",
        "students-and-exchange-visitors/f-1",
        "optional-practical-training-opt-for-f-1-students",
        "optional-practical-training-extension-for-stem",
        "/f-1-",
        "student-and-exchange-visitor",
    ),
    "us_eb5": (
        "employment-based-immigration-fifth-preference-eb-5",
        "eb-5-immigrant-investor",
        "eb-5-filing-tips",
        "eb-5-questions-and-answers",
    ),
    "us_eb1": ("employment-based-immigration-first-preference-eb-1",),
    "us_eb2": ("employment-based-immigration-second-preference-eb-2",),
    "us_eb3": ("employment-based-immigration-third-preference-eb-3",),
    "us_b1_b2": (
        "temporary-visitors-for-business/b-1-temporary-business-visitor",
        "b-1-temporary-business-visitor",
    ),
    "us_e3": ("e-3-certain-specialty-occupation-professionals-from-australia", "/e-3"),
    "au_student_500": ("visa-listing/student-500", "subclass-500"),
    "au_temporary_graduate_485": (
        "visa-listing/temporary-graduate-485",
        "temporary-graduate-485",
        # Exact owner-approved checklist tool (Phase 6M); body must still mark 485.
        "visas/web-evidentiary-tool",
    ),
    "ca_federal_skilled_worker": (
        "who-can-apply/federal-skilled-workers",
        "express-entry/documents/proof-funds",
        "express-entry/documents.html",
        "express-entry/apply-permanent-residence",
        "check-score/crs-criteria",
        "criteria-comprehensive-ranking-system",
    ),
    "ca_canadian_experience_class": (
        "who-can-apply/canadian-experience-class",
        "express-entry/documents/proof-funds",
        "express-entry/documents.html",
        "express-entry/apply-permanent-residence",
        "check-score/crs-criteria",
        "criteria-comprehensive-ranking-system",
    ),
    "ca_federal_skilled_trades": (
        "who-can-apply/federal-skilled-trades",
        "express-entry/documents/proof-funds",
        "express-entry/documents.html",
        "express-entry/apply-permanent-residence",
        "check-score/crs-criteria",
        "criteria-comprehensive-ranking-system",
    ),
    "nz_skilled_migrant": (
        "skilled-migrant-category-resident-visa",
        "skilled-residence-pathways",
        "skilled-migrant",
    ),
    "nz_essential_skills": ("essential-skills-work-visa", "work-visa/essential"),
    "nz_accredited_employer_work": (
        "accredited-employer-work-visa",
        "accredited-employer",
    ),
    "nz_student_visa": (
        "visas-for-studying-in-new-zealand",
        "study/study-visas",
        "visas-for-studying",
    ),
    "nz_citizenship_by_grant": (
        "how-to-apply-for-nz-citizenship",
        "citizenship/how-to-apply",
        "nz-citizenship/how-to-apply",
    ),
    "ie_critical_skills_employment_permit": (
        "critical-skills-employment-permit",
        "critical-skills-employment-permits-checklist",
        "employment-permits/critical-skills",
        "permit-types/critical-skills",
    ),
    "ie_general_employment_permit": (
        "general-employment-permit",
        "general-employment-permits-checklist",
        "employment-permits/general",
        "permit-types/general-employment",
    ),
    "jp_engineer_specialist": (
        "applications/status/engineer",
        "engineer_humanities",
        "engineer-humanities",
    ),
    "fr_passeport_talent": (
        "passeport-talent",
        "passeport_talent",
        "n19804",
        "vosdroits/f16922",
        "f16922",
    ),
    "fr_student": ("vosdroits/n19805", "etudiant", "étudiant"),
    "fr_salarie": (
        "vosdroits/f2728",
        "vosdroits/n110",
        "salarie",
        "salarié",
    ),
    "fr_naturalisation": ("vosdroits/n111", "naturalisation"),
    # UAE: pathway-specific visa type pages (not the generic emirates-id hub).
    "ae_work_residence": (
        "residence-visa-for-working",
        "residence-visa-for-work",
        "working-in-the-uae",
    ),
    "ae_golden_visa": ("types-of-visas/golden-visa", "golden-visa"),
    "ae_green_visa": ("types-of-visas/green-visa", "green-visa"),
    "ae_student": ("residence-visa-for-stud", "student-residence"),
    "ae_visit_visa": ("types-of-visas/visit-visa", "tourist-visa"),
    "ae_family": ("residence-visa-for-family", "family-sponsorship"),
    "us_j1": ("students-and-exchange-visitors/exchange-visitors", "/j-1", "j-1-visa"),
    "us_l1a": ("l-1a-intracompany",),
    "us_l1b": ("l-1b-intracompany",),
}

# URL substrings that must never support the pathway (cross-category false positives).
PATHWAY_HARD_EXCLUSIONS: dict[str, tuple[str, ...]] = {
    "us_h1b": (
        "international-entrepreneur",
        "entrepreneur-rule",
        "permanent-workers",
        "o-1-visa",
        "e-2-treaty",
        "students-and-exchange",
        "eb-5",
        "h-4-dependent",
        "employment-authorization-for-certain-h-4",
    ),
    "us_o1": (
        "international-entrepreneur",
        "entrepreneur-rule",
        "permanent-workers",
        "h-1b-specialty",
        "e-2-treaty",
        "students-and-exchange",
    ),
    "us_e2": (
        "international-entrepreneur",
        "entrepreneur-rule",
        "permanent-workers",
        "h-1b-specialty",
        "o-1-visa",
        "students-and-exchange",
        "eb-5",
    ),
    "us_l1": (
        "international-entrepreneur",
        "permanent-workers",
        "h-1b-specialty",
        "students-and-exchange",
    ),
    "us_eb5": (
        "temporary-workers",
        "students-and-exchange",
        "h-1b-specialty",
        "temporary-visitors-for-business",
        "stem-employment-pathways",
    ),
    "us_b1_b2": (
        "permanent-workers",
        "students-and-exchange",
        "temporary-workers",
        "h-1b-specialty",
        "eb-5",
    ),
    "us_f1": (
        "international-entrepreneur",
        "entrepreneur-rule",
        "permanent-workers",
        "eb-5",
        "h-1b-specialty",
        "o-1-visa",
        "e-2-treaty",
        "l-1a-intracompany",
        "l-1b-intracompany",
        "temporary-visitors-for-business",
        "conrad-30",
    ),
    "au_temporary_graduate_485": (
        "visa-listing/student-500",
        "student-500",
        "subclass-500",
        "visa-listing/visitor",
    ),
    "au_student_500": (
        "temporary-graduate-485",
        "subclass-485",
    ),
    "de_eu_blue_card": (
        "visa-service/215870",  # AA short-stay / Schengen hub
        "schengen-visa",
        "short-stay",
        "national-visa-for-study",
        "europa.eu/youreurope",
    ),
    "gb_student": ("skilled-worker-visa", "graduate-visa", "global-talent", "europa.eu/youreurope"),
    "gb_skilled_worker": ("student-visa", "graduate-visa", "global-talent", "europa.eu/youreurope"),
    "gb_graduate": ("student-visa", "skilled-worker-visa", "global-talent", "europa.eu/youreurope"),
    "gb_global_talent": ("student-visa", "skilled-worker-visa", "graduate-visa", "europa.eu/youreurope"),
    "gb_innovator_founder": ("student-visa", "skilled-worker-visa", "global-talent", "europa.eu/youreurope"),
    "gb_visitor": ("skilled-worker-visa", "student-visa", "europa.eu/youreurope"),
    "gb_citizenship": ("indefinite-leave-to-remain", "settle-in-the-uk", "europa.eu/youreurope"),
    "gb_settlement_ilr": ("british-citizenship", "becoming-a-british-citizen", "europa.eu/youreurope"),
    "jp_student": ("engineer", "highlyskilled", "highly-skilled"),
    "ie_critical_skills_employment_permit": ("general-employment-permit",),
    "ie_general_employment_permit": ("critical-skills-employment-permit",),
    # UAE cross-category exclusions
    "ae_work_residence": ("golden-visa", "green-visa", "visit-visa", "tourist-visa", "transit-visa"),
    "ae_golden_visa": ("residence-visa-for-working", "visit-visa", "tourist-visa", "green-visa"),
    "ae_visit_visa": ("golden-visa", "residence-visa-for-working", "green-visa"),
    "ae_student": ("golden-visa", "visit-visa", "tourist-visa"),
}

# Generic official hubs that may be background only — never pathway-specific evidence.
GENERIC_GUIDANCE_URL_MARKERS: tuple[str, ...] = (
    "/working-in-the-united-states$",
    "/working-in-the-united-states/$",
    "working-in-the-united-states$",
    "visa-service/215870",
    "information-and-services/visa-and-emirates-id",
)

# Pathway-specific body markers (used only after URL applicability passes).
PATHWAY_BODY_MARKERS: dict[str, tuple[str, ...]] = {
    "us_f1": ("f-1", "f1 student", "sevis", "form i-20", "optional practical training", "cpt"),
    "us_h1b": ("h-1b", "h1b", "specialty occupation", "labor condition application"),
    "us_o1": ("o-1", "extraordinary ability", "advisory opinion"),
    "us_e2": ("e-2", "treaty investor", "substantial investment"),
    "us_l1": ("l-1", "intracompany", "managerial capacity", "specialized knowledge"),
    "us_eb5": ("eb-5", "eb5", "immigrant investor", "form i-526", "form i-829", "regional center"),
    "us_b1_b2": ("b-1", "business visitor", "temporary business visitor"),
    "us_j1": ("j-1", "exchange visitor", "ds-2019"),
    "au_temporary_graduate_485": (
        "485",
        "temporary graduate",
        "subclass 485",
        "finished your studies",
        "post-study",
    ),
    "au_student_500": ("subclass 500", "student visa (subclass 500)"),
    "de_eu_blue_card": ("blue card", "blaue karte", "eu blue card", "hochqualifizierte"),
    "gb_skilled_worker": ("skilled worker", "certificate of sponsorship", "cos "),
    "gb_student": ("student visa", "confirmation of acceptance", "cas "),
    "gb_graduate": ("graduate visa", "post-study"),
    "gb_global_talent": ("global talent", "endorsement"),
    "gb_innovator_founder": ("innovator founder", "endorsing body"),
    "gb_visitor": ("standard visitor", "visitor visa"),
    "gb_citizenship": ("british citizen", "naturalisation", "citizenship"),
    "gb_settlement_ilr": ("indefinite leave", "settled status", "ilr"),
    "jp_student": ("student", "留学", "就学"),
    "fr_naturalisation": ("naturalisation", "nationalité", "nationalite"),
    "fr_student": ("étudiant", "etudiant", "student"),
    "fr_salarie": ("salarié", "salarie", "employee"),
    "nl_highly_skilled_migrant": ("highly skilled migrant", "kennismigrant"),
    "sg_employment_pass": ("employment pass",),
    "ie_critical_skills_employment_permit": ("critical skills",),
    "ie_general_employment_permit": ("general employment permit",),
    "ca_federal_skilled_worker": (
        "federal skilled worker", "fsw", "express entry", "invitation to apply",
    ),
    "ca_canadian_experience_class": (
        "canadian experience class", "cec", "express entry", "invitation to apply",
    ),
    "ca_federal_skilled_trades": (
        "federal skilled trades", "fst", "express entry", "invitation to apply",
    ),
    "nz_citizenship_by_grant": ("citizenship by grant", "grant of citizenship"),
    "jp_engineer_specialist": (
        "engineer",
        "specialist in humanities",
        "international services",
        "技術",
        "人文知識",
        "国際業務",
    ),
    "fr_passeport_talent": (
        "passeport talent",
        "talent passport",
        "passeport-talent",
        "carte talent",
        "talent-salarié",
        "talent-salarie",
        "carte de séjour pluriannuelle",
    ),
    "ae_work_residence": (
        "work permit",
        "residence visa for working",
        "employment visa",
        "working in the uae",
    ),
    "ae_golden_visa": ("golden visa", "10 years", "long-term residence"),
    "ae_visit_visa": ("visit visa", "tourist visa"),
    "ae_student": ("student residence", "study in the uae"),
    "nz_skilled_migrant": ("skilled migrant", "skilled residence"),
    "nz_accredited_employer_work": ("accredited employer", "aewv"),
    "nz_student_visa": ("student visa", "study in new zealand"),
}


@dataclass(frozen=True)
class ApplicabilityResult:
    applies: bool
    strength: str  # strong | shared | generic | excluded | unrelated
    basis: str
    reason: str


def url_key(url: str) -> str:
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    path = (parts.path or "").lower().rstrip("/")
    return f"{host}{path}"


def _matches_any(haystack: str, needles: tuple[str, ...]) -> str:
    for n in needles:
        if n and n.lower() in haystack:
            return n
    return ""


def document_applies_to_pathway(
    *,
    pathway_id: str,
    source_url: str,
    title: str = "",
    official_url: str = "",
    body_text: str = "",
    require_body_markers: bool = False,
) -> ApplicabilityResult:
    """Decide whether a document may support a pathway-specific requirement.

    Order: hard exclusion → URL tokens / official scope → reject generic hubs.
    Body markers are optional and only enforced when require_body_markers=True
    (fresh machine assessment over full extraction text). Short stored passages
    must not cause curated pathway-URL evidence to be rejected.
    """
    key = url_key(source_url)
    title_l = (title or "").lower()
    combined = f"{key} {title_l}"

    excluded = _matches_any(combined, PATHWAY_HARD_EXCLUSIONS.get(pathway_id, ()))
    if excluded:
        return ApplicabilityResult(
            False, "excluded", f"hard_exclusion:{excluded}",
            f"Document matches excluded category marker '{excluded}' for {pathway_id}",
        )

    token_hit = _matches_any(key, PATHWAY_URL_TOKENS.get(pathway_id, ()))
    title_token_hit = _matches_any(title_l, PATHWAY_URL_TOKENS.get(pathway_id, ()))
    official_hit = False
    if official_url:
        off = url_key(official_url)
        if off and (key == off or key.startswith(off + "/") or off.startswith(key + "/")):
            official_hit = True

    if token_hit or title_token_hit or official_hit:
        # Still reject bare generic hubs (no pathway-specific token beyond the hub itself).
        for marker in GENERIC_GUIDANCE_URL_MARKERS:
            pattern = marker[:-1] if marker.endswith("$") else marker
            is_hub = bool(re.search(marker, key)) if marker.endswith("$") else (pattern in key)
            if not is_hub:
                continue
            # Pathway-specific token that is more specific than the hub path → allow.
            if token_hit and token_hit not in pattern and pattern not in token_hit:
                continue
            if not token_hit:
                return ApplicabilityResult(
                    False, "generic", f"generic_hub:{marker}",
                    "Generic official guidance page cannot establish pathway-specific requirements",
                )

        basis = (
            f"url_token:{token_hit}"
            if token_hit
            else (f"title_token:{title_token_hit}" if title_token_hit else "official_url_scope")
        )
        if require_body_markers:
            markers = PATHWAY_BODY_MARKERS.get(pathway_id, ())
            if markers and body_text:
                body_l = body_text.lower()
                if not _matches_any(body_l, markers) and not _matches_any(title_l, markers):
                    return ApplicabilityResult(
                        False, "unrelated", "missing_pathway_marker",
                        f"URL looks related but body lacks pathway markers for {pathway_id}",
                    )
        return ApplicabilityResult(True, "strong", basis, "Pathway-specific URL scope")

    # No URL scope: never allow keyword-only upgrades.
    return ApplicabilityResult(
        False, "unrelated", "no_pathway_url_scope",
        "Document URL/title does not establish pathway-specific applicability",
    )


def is_generic_guidance_url(url: str) -> bool:
    key = url_key(url)
    for marker in GENERIC_GUIDANCE_URL_MARKERS:
        if marker.endswith("$"):
            if re.search(marker, key):
                return True
        elif marker in key:
            return True
    return False
