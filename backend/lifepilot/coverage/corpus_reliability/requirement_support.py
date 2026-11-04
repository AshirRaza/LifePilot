"""Requirement-level passage support checks (Phase 6K).

Pathway URL match is necessary but not sufficient. A passage must also
meaningfully address the specific requirement — without treating exemptions
as positive requirements, or mis-filing financial text as document checklists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RequirementSupport:
    ok: bool
    kind: str  # supports | exemption | mismatch | insufficient | wrong_category
    reason: str
    suggested_classification: str | None = None  # e.g. NOT_APPLICABLE for exemptions
    suggested_requirement: str | None = None


# Phrases that indicate an exemption / non-applicability rather than a positive requirement.
EXEMPTION_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "financial_or_sponsorship": [
        re.compile(r"don.?t need proof of funds", re.I),
        re.compile(r"do not need proof of funds", re.I),
        re.compile(r"who doesn.?t need proof of funds", re.I),
        re.compile(r"not required.*(proof of funds|funds)", re.I),
        re.compile(r"exempt.*(proof of funds|funds)", re.I),
        re.compile(r"applying under the canadian experience class", re.I),
    ],
    "required_documents": [
        re.compile(r"don.?t need (?:to (?:provide|submit) )?(?:any )?documents?", re.I),
    ],
}

# Strong positive cues that the passage addresses the requirement type.
POSITIVE_CUES: dict[str, list[re.Pattern[str]]] = {
    "eligibility": [
        re.compile(r"\beligib", re.I),
        re.compile(r"who can (?:apply|qualify)", re.I),
        re.compile(r"you must (?:be|have|demonstrate|meet)", re.I),
        re.compile(r"qualif(?:y|ication|ications)\b", re.I),
        re.compile(r"criteria\b", re.I),
        re.compile(r"to qualify\b", re.I),
        re.compile(r"remplissez les conditions", re.I),
        re.compile(r"conditions de d[eé]livrance", re.I),
        re.compile(r"vous pouvez obtenir", re.I),
        re.compile(r"si vous remplissez", re.I),
    ],
    "required_documents": [
        re.compile(r"documents? (?:you|required|needed|to (?:provide|submit|bring))", re.I),
        re.compile(r"supporting documents?", re.I),
        re.compile(r"checklist", re.I),
        re.compile(r"you(?:'| wi)?ll need to (?:provide|submit|upload)", re.I),
        re.compile(r"evidence you need", re.I),
        re.compile(r"prepare the evidence", re.I),
        re.compile(r"documents? [aà] fournir", re.I),
        re.compile(r"pi[eè]ces? [aà] fournir", re.I),
        re.compile(r"pr[eé]parer les documents", re.I),
        re.compile(r"attestation employeur", re.I),
    ],
    "application_process": [
        re.compile(r"how to apply", re.I),
        re.compile(r"application process", re.I),
        re.compile(r"apply (?:online|from|to|for)", re.I),
        re.compile(r"submit (?:your )?(?:application|form|petition)", re.I),
        re.compile(r"steps? (?:to|in) (?:apply|application)", re.I),
        re.compile(r"filing\b", re.I),
        re.compile(r"d[eé]poser votre demande", re.I),
        re.compile(r"faire la d[eé]marche", re.I),
        re.compile(r"[eé]tapes? de la d[eé]marche", re.I),
        re.compile(r"demande de (?:visa|titre de s[eé]jour)", re.I),
    ],
    "financial_or_sponsorship": [
        re.compile(r"proof of funds", re.I),
        re.compile(r"financial requirement", re.I),
        re.compile(r"maintenance funds", re.I),
        re.compile(r"salary (?:threshold|requirement|benchmark)", re.I),
        re.compile(r"sponsor(?:ship)?\b", re.I),
        re.compile(r"enough money", re.I),
        re.compile(r"course fee", re.I),
        re.compile(r"qualifying salary", re.I),
        re.compile(r"r[eé]mun[eé]ration", re.I),
        re.compile(r"seuil de salaire", re.I),
    ],
}

# Content that belongs to another requirement (mis-filed associations).
MISFILE_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # proof-of-funds content filed under required_documents
    (
        "required_documents",
        re.compile(r"proof of funds|who needs proof of funds|settlement funds", re.I),
        "financial_or_sponsorship",
    ),
    # money/financial requirement filed under eligibility
    (
        "eligibility",
        re.compile(
            r"^(?=.*(?:money to support|financial requirement|course fee|proof of funds))"
            r"(?!.*(?:eligib|who can|criteria|qualif|you must be|to qualify)).*$",
            re.I | re.S,
        ),
        "financial_or_sponsorship",
    ),
]

# Known wrong-category URL/title markers (legally distinct subclasses).
WRONG_CATEGORY: dict[str, list[re.Pattern[str]]] = {
    "us_e2": [
        re.compile(r"e-2-visa-cnmi-only", re.I),
        re.compile(r"\bcnmi\b", re.I),
        re.compile(r"commonwealth of the northern mariana", re.I),
    ],
    "us_f1": [
        # Only reject pages that are specifically other student categories —
        # shared "students and employment" pages may mention M-1/J-1 in passing.
        re.compile(r"/m-1-|m-1-vocational|m-1-student", re.I),
        re.compile(r"/j-1-|j-1-exchange", re.I),
    ],
}

# Document-list substance: prefer multiple item cues for required_documents PARTIAL.
DOC_ITEM_CUES = re.compile(
    r"passport|birth certificate|bank statement|transcript|diploma|degree|"
    r"job offer|cos\b|cas\b|i-20|i-94|petition|form i-|police|biometric|"
    r"qualification|acra|photograph|photo\b|resume|cv\b|checklist|"
    r"passeport|attestation|contrat de travail|dipl[oô]me|justificatif|"
    r"photo d.identit[eé]|extrait de casier",
    re.I,
)

MIN_PASSAGE = 50


def _has_positive_cue(passage: str, requirement: str) -> bool:
    return any(p.search(passage) for p in POSITIVE_CUES.get(requirement, []))


def _exemption_hit(passage: str, requirement: str) -> re.Pattern[str] | None:
    for p in EXEMPTION_PATTERNS.get(requirement, []):
        if p.search(passage):
            return p
    # Cross-check: proof-of-funds exemption text under required_documents
    if requirement == "required_documents":
        for p in EXEMPTION_PATTERNS.get("financial_or_sponsorship", []):
            if p.search(passage) and re.search(r"proof of funds|canadian experience class", passage, re.I):
                return p
    return None


def _misfile_target(passage: str, requirement: str) -> str | None:
    for req, pattern, target in MISFILE_PATTERNS:
        if req != requirement:
            continue
        if pattern.search(passage):
            # Only treat as misfile when positive cues for the claimed requirement are weak
            # or the financial/exemption language dominates.
            if requirement == "required_documents" and re.search(r"proof of funds", passage, re.I):
                if not re.search(r"checklist|supporting documents|documents you need", passage, re.I):
                    return target
            if requirement == "eligibility":
                if _has_positive_cue(passage, "eligibility") and not re.search(
                    r"^(?=.*money to support|financial requirement)", passage, re.I | re.S
                ):
                    # eligibility with incidental money mention can still be OK if eligibility cues dominate
                    elig = len(re.findall(r"eligib|criteria|qualif|who can", passage, re.I))
                    fin = len(re.findall(r"money|financial|funds|fee", passage, re.I))
                    if elig >= fin:
                        return None
                return target
    return None


def passage_supports_requirement(
    *,
    pathway_id: str,
    requirement: str,
    passage: str,
    source_url: str = "",
    title: str = "",
) -> RequirementSupport:
    """Decide whether a passage supports the claimed requirement for a pathway."""
    text = (passage or "").strip()
    if requirement == "official_provenance":
        return RequirementSupport(True, "supports", "provenance_not_content_gated")

    if len(text) < MIN_PASSAGE:
        return RequirementSupport(False, "insufficient", f"passage_too_short:{len(text)}")

    combined = f"{source_url} {title} {text}"
    for pat in WRONG_CATEGORY.get(pathway_id, []):
        if pat.search(combined):
            return RequirementSupport(
                False,
                "wrong_category",
                f"passage_or_url_indicates_distinct_category:{pat.pattern}",
            )

    # Exemptions: preserve as NOT_APPLICABLE for the *financial* requirement when clear;
    # never as positive required_documents evidence.
    if requirement == "required_documents":
        ex = _exemption_hit(text, "required_documents")
        if ex is None and re.search(r"proof of funds", text, re.I):
            # CEC / POF exemption pages are not document checklists
            if re.search(
                r"don.?t need proof of funds|who doesn.?t need proof of funds|"
                r"canadian experience class|who needs proof of funds",
                text,
                re.I,
            ):
                return RequirementSupport(
                    False,
                    "mismatch",
                    "proof_of_funds_page_is_not_required_documents_checklist",
                    suggested_classification="NOT_APPLICABLE",
                    suggested_requirement="financial_or_sponsorship",
                )

    if requirement in ("financial_or_sponsorship", "required_documents"):
        # Explicit CEC exemption for financial
        if pathway_id == "ca_canadian_experience_class" and re.search(
            r"canadian experience class|don.?t need proof of funds|who doesn.?t need",
            text,
            re.I,
        ):
            if requirement == "financial_or_sponsorship":
                return RequirementSupport(
                    True,
                    "exemption",
                    "CEC exempt from proof of funds under IRCC guidance",
                    suggested_classification="NOT_APPLICABLE",
                    suggested_requirement="financial_or_sponsorship",
                )
            return RequirementSupport(
                False,
                "mismatch",
                "CEC_proof_of_funds_exemption_is_not_required_documents",
                suggested_classification="NOT_APPLICABLE",
                suggested_requirement="financial_or_sponsorship",
            )

    mis = _misfile_target(text, requirement)
    if mis:
        return RequirementSupport(
            False,
            "mismatch",
            f"passage_belongs_to_{mis}_not_{requirement}",
            suggested_requirement=mis,
        )

    if not _has_positive_cue(text, requirement):
        return RequirementSupport(
            False,
            "insufficient",
            f"no_positive_requirement_cue_for_{requirement}",
        )

    if requirement == "required_documents":
        items = DOC_ITEM_CUES.findall(text)
        if len(set(i.lower() for i in items)) < 2 and not re.search(r"checklist", text, re.I):
            # Single document mention is weak for a documents requirement
            if not re.search(
                r"documents? you|evidence you need|prepare the evidence|"
                r"documents? [aà] fournir|pi[eè]ces? [aà] fournir|pr[eé]parer les documents",
                text,
                re.I,
            ):
                return RequirementSupport(
                    False,
                    "insufficient",
                    "required_documents_lacks_checklist_or_multiple_document_types",
                )

    # H-1B "financial" that is only LCA/attestation language without salary/funds
    if requirement == "financial_or_sponsorship" and pathway_id.startswith("us_"):
        if re.search(r"attestation|labor condition application|\bLCA\b", text, re.I):
            if not re.search(r"salary|wage|prevailing|funds|sponsor", text, re.I):
                return RequirementSupport(
                    False,
                    "insufficient",
                    "lca_attestation_alone_is_not_financial_requirement_evidence",
                )

    # NZ income-for-points is not settlement funds / sponsorship
    if requirement == "financial_or_sponsorship" and pathway_id.startswith("nz_"):
        if re.search(r"claim points|points for", text, re.I) and not re.search(
            r"funds|sponsor|settlement|maintenance", text, re.I
        ):
            return RequirementSupport(
                False,
                "mismatch",
                "income_points_are_not_financial_or_sponsorship_requirement",
            )

    return RequirementSupport(True, "supports", "passage_supports_requirement")


def validate_evidence_claim(
    *,
    pathway_id: str,
    requirement: str,
    passage: str,
    source_url: str = "",
    title: str = "",
    classification: str = "",
) -> RequirementSupport:
    """Validate a positive evidence claim (PARTIAL/COMPLETE)."""
    if classification in ("", "UNEVALUATED", "MISSING", "BLOCKED"):
        return RequirementSupport(True, "supports", "no_positive_claim")
    if classification == "NOT_APPLICABLE":
        # N/A still needs an explicit rationale in the passage/explanation upstream
        return RequirementSupport(True, "exemption", "not_applicable_preserved")
    return passage_supports_requirement(
        pathway_id=pathway_id,
        requirement=requirement,
        passage=passage,
        source_url=source_url,
        title=title,
    )
