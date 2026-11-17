"""Build Phase 9D.3 claim-level verification reliability fixtures."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "config" / "eval" / "verification_reliability.json"


def syn(
    fid: str,
    *,
    category: str,
    jurisdiction: str,
    pathway_id: str | None,
    question: str,
    evidence: list[dict],
    claims: list[dict],
    expected: list[str],
    retained: list[bool] | None = None,
    raw_answer: str | None = None,
    missing: list[str] | None = None,
    forbidden: list[str] | None = None,
    required: list[str] | None = None,
) -> dict:
    return {
        "id": fid,
        "kind": "synthetic",
        "category": category,
        "jurisdiction": jurisdiction,
        "pathway_id": pathway_id,
        "question": question,
        "evidence": evidence,
        "claims": claims,
        "raw_answer": raw_answer
        or " ".join(c["claim"] for c in claims),
        "missing_information": missing or [],
        "expected_claim_statuses": expected,
        "expected_retained": retained
        if retained is not None
        else [s not in {"contradicted", "insufficient_evidence", "pathway_mismatch"} for s in expected],
        "forbidden_in_answer": forbidden or [],
        "required_in_answer": required or [],
        "reference_status": "synthetic_stipulated",
    }


def real(
    fid: str,
    *,
    category: str,
    jurisdiction: str,
    pathway_id: str | None,
    question: str,
    evidence: list[dict],
    claims: list[dict],
    expected: list[str],
    retained: list[bool] | None = None,
    raw_answer: str | None = None,
    missing: list[str] | None = None,
    forbidden: list[str] | None = None,
    required: list[str] | None = None,
    reference_status: str = "machine_captured_unverified",
    notes: str = "",
) -> dict:
    row = syn(
        fid,
        category=category,
        jurisdiction=jurisdiction,
        pathway_id=pathway_id,
        question=question,
        evidence=evidence,
        claims=claims,
        expected=expected,
        retained=retained,
        raw_answer=raw_answer,
        missing=missing,
        forbidden=forbidden,
        required=required,
    )
    row["kind"] = "real_official"
    row["reference_status"] = reference_status
    if notes:
        row["notes"] = notes
    return row


def ev(
    text: str,
    url: str,
    jur: str = "GB",
    *,
    external: bool = False,
    doc: int = 100,
    chunk: int = 1,
) -> dict:
    return {
        "passage_text": text,
        "source_url": url,
        "jurisdiction": jur,
        "source_type": "external" if external else "internal",
        "document_id": doc,
        "document_version_id": doc + 1000,
        "chunk_id": chunk,
        "provider": "fixture" if external else None,
    }


def claim(text: str, idxs: list[int] | None = None, uncertainty: str | None = None) -> dict:
    return {"claim": text, "evidence_index": idxs or [0], "uncertainty": uncertainty}


def build() -> dict:
    f: list[dict] = []

    # --- Correct salary with applicable evidence ---
    f.append(
        syn(
            "SYN-001",
            category="correct_salary",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="What is the Alpha Visa salary threshold?",
            evidence=[
                ev(
                    "Alpha Visa rules: the general salary threshold is £41,700 per year "
                    "or the going rate for the occupation, whichever is higher.",
                    "https://example.test/gb/alpha-visa",
                )
            ],
            claims=[
                claim(
                    "The Alpha Visa general salary threshold is £41,700 per year "
                    "or the going rate, whichever is higher."
                )
            ],
            expected=["supported"],
            required=["£41,700"],
        )
    )
    f.append(
        syn(
            "SYN-002",
            category="correct_salary",
            jurisdiction="NL",
            pathway_id="nl_highly_skilled_migrant",
            question="What is the under-30 salary criterion?",
            evidence=[
                ev(
                    "Highly skilled migrant: for applicants under 30 the monthly "
                    "salary criterion is €2,801 excluding holiday allowance.",
                    "https://example.test/nl/hsm",
                    "NL",
                )
            ],
            claims=[
                claim(
                    "For highly skilled migrants under 30 the monthly salary "
                    "criterion is €2,801 excluding holiday allowance."
                )
            ],
            expected=["supported"],
            required=["€2,801"],
        )
    )

    # --- Incorrect salary ---
    f.append(
        syn(
            "SYN-003",
            category="incorrect_salary",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Alpha Visa salary?",
            evidence=[
                ev(
                    "Alpha Visa rules: the general salary threshold is £41,700 per year.",
                    "https://example.test/gb/alpha-visa",
                )
            ],
            claims=[claim("The Alpha Visa general salary threshold is £99,999 per year.")],
            expected=["insufficient_evidence"],
            retained=[False],
            forbidden=["£99,999"],
        )
    )
    f.append(
        syn(
            "SYN-004",
            category="incorrect_salary",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Critical Skills salary?",
            evidence=[
                ev(
                    "Critical Skills Employment Permit: remuneration must be at least "
                    "€38,000 for some Critical Skills occupations.",
                    "https://example.test/ie/critical-skills-employment-permit",
                    "IE",
                )
            ],
            claims=[
                claim(
                    "The Critical Skills Employment Permit minimum remuneration "
                    "is €12,000."
                )
            ],
            expected=["insufficient_evidence"],
            retained=[False],
            forbidden=["€12,000"],
        )
    )

    # --- Wrong pathway attribution ---
    f.append(
        syn(
            "SYN-005",
            category="wrong_pathway_attribution",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Critical Skills remuneration?",
            evidence=[
                ev(
                    "General Employment Permit remuneration criteria. "
                    "Minimum annual remuneration of €68,911 for eligible employment.",
                    "https://example.test/ie/general-employment-permit",
                    "IE",
                    doc=201,
                ),
                ev(
                    "Critical Skills Employment Permit eligibility depends on "
                    "occupation type and proposed remuneration level.",
                    "https://example.test/ie/critical-skills-employment-permit",
                    "IE",
                    doc=202,
                    chunk=2,
                ),
            ],
            claims=[
                claim(
                    "The Critical Skills Employment Permit minimum annual "
                    "remuneration is €68,911.",
                    [0, 1],
                )
            ],
            expected=["pathway_mismatch"],
            retained=[False],
            forbidden=["€68,911"],
        )
    )
    f.append(
        syn(
            "SYN-006",
            category="wrong_pathway_attribution",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Critical Skills pay?",
            evidence=[
                ev(
                    "General Employment Permit minimum annual remuneration of €32,691.",
                    "https://example.test/ie/general-employment-permit",
                    "IE",
                )
            ],
            claims=[
                claim(
                    "The Critical Skills Employment Permit threshold is €32,691."
                )
            ],
            expected=["pathway_mismatch"],
            retained=[False],
            forbidden=["€32,691"],
        )
    )

    # --- Valid pathway comparison ---
    f.append(
        syn(
            "SYN-007",
            category="valid_pathway_comparison",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Compare Critical Skills and GEP pay?",
            evidence=[
                ev(
                    "General Employment Permit remuneration criteria include "
                    "thresholds of €32,691. Critical Skills Employment Permit "
                    "is a different permit type.",
                    "https://example.test/ie/general-employment-permit",
                    "IE",
                )
            ],
            claims=[
                claim(
                    "Unlike the Critical Skills Employment Permit, the General "
                    "Employment Permit has a separate remuneration threshold of €32,691."
                )
            ],
            expected=["supported"],
            required=["€32,691"],
        )
    )
    f.append(
        syn(
            "SYN-008",
            category="valid_pathway_comparison",
            jurisdiction="IE",
            pathway_id="ie_general_employment_permit",
            question="How do GEP and Critical Skills differ?",
            evidence=[
                ev(
                    "General Employment Permits and Critical Skills Employment Permits "
                    "are distinct permit types with separate remuneration rules. "
                    "General Employment Permit threshold example: €34,009.",
                    "https://example.test/ie/general-employment-permit",
                    "IE",
                )
            ],
            claims=[
                claim(
                    "General Employment Permits and Critical Skills Employment Permits "
                    "are distinct permit types; the General Employment Permit has a "
                    "remuneration threshold of €34,009."
                )
            ],
            expected=["supported"],
        )
    )

    # --- Wrong jurisdiction ---
    f.append(
        syn(
            "SYN-009",
            category="wrong_jurisdiction",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Ireland salary?",
            evidence=[
                ev(
                    "Skilled Worker visa general minimum salary is £41,700.",
                    "https://www.gov.uk/skilled-worker-visa",
                    "GB",
                )
            ],
            claims=[
                claim(
                    "Ireland Critical Skills uses the UK Skilled Worker threshold "
                    "of £41,700."
                )
            ],
            expected=["insufficient_evidence"],
            retained=[False],
            forbidden=["£41,700"],
        )
    )
    f.append(
        syn(
            "SYN-010",
            category="wrong_jurisdiction",
            jurisdiction="NL",
            pathway_id="nl_highly_skilled_migrant",
            question="NL HSM salary?",
            evidence=[
                ev(
                    "US H-1B specialty occupation wages vary by location.",
                    "https://example.test/us/h1b",
                    "US",
                )
            ],
            claims=[claim("The Dutch highly skilled migrant salary is $60,000.")],
            expected=["insufficient_evidence"],
            retained=[False],
            forbidden=["$60,000"],
        )
    )

    # --- Fabricated fees ---
    f.append(
        syn(
            "SYN-011",
            category="fabricated_fee",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Application fee?",
            evidence=[
                ev(
                    "Alpha Visa page describes eligibility and sponsorship. "
                    "Fee table is not included in this excerpt.",
                    "https://example.test/gb/alpha-visa",
                )
            ],
            claims=[claim("The Alpha Visa application fee is exactly £1,234.")],
            expected=["insufficient_evidence"],
            retained=[False],
            forbidden=["£1,234"],
        )
    )
    f.append(
        syn(
            "SYN-012",
            category="correct_fee",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Fee from outside UK?",
            evidence=[
                ev(
                    "Fees: applying for an Alpha Visa from outside the UK costs £719 "
                    "for up to 3 years.",
                    "https://example.test/gb/alpha-visa-fees",
                )
            ],
            claims=[
                claim(
                    "Applying for an Alpha Visa from outside the UK costs £719 "
                    "for up to 3 years."
                )
            ],
            expected=["supported"],
            required=["£719"],
        )
    )

    # --- Correct fees with missing conditions ---
    f.append(
        syn(
            "SYN-013",
            category="fee_missing_conditions",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="What is the fee?",
            evidence=[
                ev(
                    "Fees: applying from outside the UK costs £719 for up to 3 years. "
                    "A higher fee applies for longer permission. Healthcare surcharge "
                    "is charged separately.",
                    "https://example.test/gb/alpha-visa-fees",
                )
            ],
            claims=[
                claim("Applying for an Alpha Visa from outside the UK costs £719.")
            ],
            expected=["supported"],
            missing=[
                "Whether the permission length is up to 3 years",
                "Whether the healthcare surcharge applies in addition",
            ],
            # Fee itself is supported; omission is tracked via missing_information
            # rather than claim rejection (verifier limitation for soft omissions).
        )
    )

    # --- Future-effective ---
    f.append(
        syn(
            "SYN-014",
            category="future_effective",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Current salary threshold?",
            evidence=[
                ev(
                    "From 1 April 2027 the Alpha Visa general salary threshold will be "
                    "£50,000. Do not treat the future figure as currently applicable.",
                    "https://example.test/gb/alpha-visa",
                )
            ],
            claims=[
                claim("The current Alpha Visa general salary threshold is £50,000.")
            ],
            expected=["temporal_uncertainty"],
            retained=[True],
        )
    )
    f.append(
        syn(
            "SYN-015",
            category="future_effective",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="When does the new threshold apply?",
            evidence=[
                ev(
                    "With effect from 1 January 2028, the maintenance funds rule "
                    "will require £2,000.",
                    "https://example.test/gb/student",
                )
            ],
            claims=[
                claim(
                    "The Student maintenance funds requirement is currently £2,000."
                )
            ],
            expected=["temporal_uncertainty"],
            retained=[True],
        )
    )

    # --- Historical / superseded ---
    f.append(
        syn(
            "SYN-016",
            category="historical_as_current",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Current salary?",
            evidence=[
                ev(
                    "Previously the Alpha Visa threshold was £26,200. That historical "
                    "figure no longer applies under current guidance.",
                    "https://example.test/gb/alpha-visa",
                )
            ],
            claims=[claim("The Alpha Visa salary threshold is £26,200.")],
            expected=["temporal_uncertainty"],
            retained=[True],
        )
    )
    f.append(
        syn(
            "SYN-017",
            category="correct_historical",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="What was the former threshold?",
            evidence=[
                ev(
                    "Previously the Alpha Visa threshold was £26,200. That historical "
                    "figure no longer applies under current guidance.",
                    "https://example.test/gb/alpha-visa",
                )
            ],
            claims=[
                claim(
                    "Previously the Alpha Visa threshold was £26,200 and that "
                    "historical figure no longer applies."
                )
            ],
            expected=["supported"],
            required=["£26,200"],
        )
    )

    # --- Eligibility exceptions / work rights ---
    f.append(
        syn(
            "SYN-018",
            category="eligibility_exception",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Are there exceptions?",
            evidence=[
                ev(
                    "Alpha Visa: lower salary rules may apply if extending an existing "
                    "visa or working in listed healthcare occupations, subject to "
                    "conditions.",
                    "https://example.test/gb/alpha-visa",
                )
            ],
            claims=[
                claim(
                    "Lower Alpha Visa salary rules may apply when extending an "
                    "existing visa or for listed healthcare occupations, subject "
                    "to conditions."
                )
            ],
            expected=["supported"],
        )
    )
    f.append(
        syn(
            "SYN-019",
            category="work_rights",
            jurisdiction="GB",
            pathway_id="gb_student",
            question="Can students work?",
            evidence=[
                ev(
                    "Student permission: term-time work is limited to 20 hours per week "
                    "for degree-level courses unless the condition says otherwise.",
                    "https://example.test/gb/student",
                )
            ],
            claims=[
                claim(
                    "Student permission generally limits term-time work to 20 hours "
                    "per week for degree-level courses unless the condition says otherwise."
                )
            ],
            expected=["supported"],
            required=["20 hours"],
        )
    )
    f.append(
        syn(
            "SYN-020",
            category="work_rights",
            jurisdiction="GB",
            pathway_id="gb_student",
            question="Unlimited work?",
            evidence=[
                ev(
                    "Student permission: term-time work is limited to 20 hours per week "
                    "for degree-level courses.",
                    "https://example.test/gb/student",
                )
            ],
            claims=[
                claim("Students may work unlimited hours during term time.")
            ],
            expected=["contradicted"],
            retained=[False],
            forbidden=["unlimited"],
        )
    )

    # --- Contradictory official passages ---
    f.append(
        syn(
            "SYN-021",
            category="contradictory_passages",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Salary?",
            evidence=[
                ev(
                    "Alpha Visa general salary threshold is £41,700.",
                    "https://example.test/gb/alpha-a",
                    doc=301,
                ),
                ev(
                    "Alpha Visa general salary threshold is exactly £38,700 only; "
                    "£41,700 does not apply.",
                    "https://example.test/gb/alpha-b",
                    doc=302,
                    chunk=2,
                ),
            ],
            claims=[
                claim(
                    "The Alpha Visa general salary threshold is only exactly £41,700.",
                    [0, 1],
                )
            ],
            expected=["contradicted"],
            retained=[False],
        )
    )

    # --- Incomplete evidence ---
    f.append(
        syn(
            "SYN-022",
            category="incomplete_evidence",
            jurisdiction="FR",
            pathway_id="fr_passeport_talent",
            question="What are all Talent card categories?",
            evidence=[
                ev(
                    "Carte talent is a multi-year residence permit. Full category "
                    "definitions are not included in this excerpt.",
                    "https://example.test/fr/carte-talent",
                    "FR",
                )
            ],
            claims=[
                claim(
                    "The carte talent has exactly twelve categories with fixed "
                    "fees of €500 each."
                )
            ],
            expected=["insufficient_evidence"],
            retained=[False],
            forbidden=["€500"],
        )
    )
    f.append(
        syn(
            "SYN-023",
            category="incomplete_evidence",
            jurisdiction="SG",
            pathway_id="sg_employment_pass",
            question="Qualifying salary?",
            evidence=[
                ev(
                    "Employment Pass overview page. Qualifying salary details are "
                    "not stated in this excerpt.",
                    "https://example.test/sg/ep",
                    "SG",
                )
            ],
            claims=[claim("The Employment Pass qualifying salary is S$5,000.")],
            expected=["insufficient_evidence"],
            retained=[False],
            forbidden=["S$5,000", "5000"],
        )
    )

    # --- Correctly qualified uncertainty ---
    f.append(
        syn(
            "SYN-024",
            category="qualified_uncertainty",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Exact fee?",
            evidence=[
                ev(
                    "Fee information depends on location and length; consult the "
                    "fee table for the applicable amount.",
                    "https://example.test/gb/fees",
                )
            ],
            claims=[
                claim(
                    "Official fee guidance indicates the amount depends on location "
                    "and length; the exact figure is not stated in the retrieved excerpt.",
                    uncertainty="Exact fee not in excerpt",
                )
            ],
            expected=["supported"],
        )
    )

    # --- Unpublished / unavailable ---
    f.append(
        syn(
            "SYN-025",
            category="unpublished",
            jurisdiction="FR",
            pathway_id=None,
            question="Unpublished Home Office scoring notes for French salarié?",
            evidence=[
                ev(
                    "Service Public page on work authorisation. No scoring methodology "
                    "or unpublished notes are described.",
                    "https://example.test/fr/autorisation",
                    "FR",
                )
            ],
            claims=[
                claim(
                    "French salarié permits are scored using unpublished Home Office "
                    "notes with a 75-point cutoff."
                )
            ],
            expected=["insufficient_evidence"],
            retained=[False],
            forbidden=["75-point", "75"],
        )
    )

    # --- Unsupported claim inside otherwise correct answer ---
    f.append(
        syn(
            "SYN-026",
            category="mixed_supported_unsupported",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Critical Skills requirements?",
            evidence=[
                ev(
                    "Critical Skills Employment Permit. Remuneration must be at least "
                    "€38,000 for some Critical Skills occupations.",
                    "https://example.test/ie/critical-skills-employment-permit",
                    "IE",
                )
            ],
            claims=[
                claim(
                    "Some Critical Skills Employment Permit occupations require "
                    "remuneration of at least €38,000."
                ),
                claim(
                    "The Critical Skills Employment Permit application fee is "
                    "exactly €9,999."
                ),
            ],
            expected=["supported", "insufficient_evidence"],
            retained=[True, False],
            required=["€38,000"],
            forbidden=["€9,999"],
            raw_answer=(
                "Some Critical Skills Employment Permit occupations require "
                "remuneration of at least €38,000. The Critical Skills Employment "
                "Permit application fee is exactly €9,999."
            ),
        )
    )

    # --- Misleading figures in gap / uncertainty sentences ---
    f.append(
        syn(
            "SYN-027",
            category="gap_prose_leak",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Critical Skills salary?",
            evidence=[
                ev(
                    "Critical Skills Employment Permit eligibility depends on "
                    "occupation and remuneration. No numeric threshold is stated.",
                    "https://example.test/ie/critical-skills-employment-permit",
                    "IE",
                ),
                ev(
                    "General Employment Permit minimum annual remuneration of €68,911.",
                    "https://example.test/ie/general-employment-permit",
                    "IE",
                    doc=401,
                    chunk=2,
                ),
            ],
            claims=[
                claim(
                    "The Critical Skills Employment Permit minimum is €68,911.",
                    [0, 1],
                )
            ],
            expected=["pathway_mismatch"],
            retained=[False],
            missing=[
                "Whether the €68,911 figure is the current official Critical Skills threshold"
            ],
            forbidden=["€68,911", "68,911"],
            raw_answer=(
                "Eligibility depends on occupation. The Critical Skills Employment "
                "Permit minimum is €68,911."
            ),
        )
    )

    # --- Missing material eligibility conditions ---
    f.append(
        syn(
            "SYN-028",
            category="missing_material_conditions",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Am I eligible with a job offer?",
            evidence=[
                ev(
                    "Alpha Visa requires an approved sponsor, a certificate of "
                    "sponsorship, an eligible occupation, and the salary threshold.",
                    "https://example.test/gb/alpha-visa",
                )
            ],
            claims=[
                claim(
                    "A job offer alone makes an applicant fully eligible for an Alpha Visa."
                )
            ],
            # Eligibility conclusion / unsupported — dropped as insufficient or via
            # grounding-style rejection; verifier may classify insufficient.
            expected=["insufficient_evidence"],
            retained=[False],
        )
    )

    # --- Unrelated but factually correct statements ---
    f.append(
        syn(
            "SYN-029",
            category="unrelated_correct",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Alpha Visa salary?",
            evidence=[
                ev(
                    "Alpha Visa general salary threshold is £41,700. Separately, "
                    "passport photos must meet identity document standards.",
                    "https://example.test/gb/alpha-visa",
                )
            ],
            claims=[
                claim("The Alpha Visa general salary threshold is £41,700."),
                claim(
                    "Passport photos must meet identity document standards."
                ),
            ],
            expected=["supported", "supported"],
            retained=[True, True],
        )
    )

    # More synthetic coverage ---
    for i, (amt, bad) in enumerate(
        [("£30,000", "£30,001"), ("€40,000", "€40,001"), ("$55,000", "$55,001")],
        start=30,
    ):
        f.append(
            syn(
                f"SYN-{i:03d}",
                category="correct_salary",
                jurisdiction="GB" if amt.startswith("£") else ("NL" if amt.startswith("€") else "US"),
                pathway_id="gb_skilled_worker"
                if amt.startswith("£")
                else (
                    "nl_highly_skilled_migrant"
                    if amt.startswith("€")
                    else None
                ),
                question="Threshold?",
                evidence=[
                    ev(
                        f"Pathway stipend criterion is {amt} under the stipulated test rules.",
                        f"https://example.test/syn/{i}",
                        "GB"
                        if amt.startswith("£")
                        else ("NL" if amt.startswith("€") else "US"),
                    )
                ],
                claims=[claim(f"The pathway stipend criterion is {amt} under the stipulated test rules.")],
                expected=["supported"],
                required=[amt],
            )
        )
        f.append(
            syn(
                f"SYN-{i+3:03d}",
                category="incorrect_salary",
                jurisdiction="GB" if amt.startswith("£") else ("NL" if amt.startswith("€") else "US"),
                pathway_id="gb_skilled_worker"
                if amt.startswith("£")
                else (
                    "nl_highly_skilled_migrant"
                    if amt.startswith("€")
                    else None
                ),
                question="Threshold?",
                evidence=[
                    ev(
                        f"Pathway stipend criterion is {amt} under the stipulated test rules.",
                        f"https://example.test/syn/{i}-src",
                        "GB"
                        if amt.startswith("£")
                        else ("NL" if amt.startswith("€") else "US"),
                    )
                ],
                claims=[claim(f"The pathway stipend criterion is {bad}.")],
                expected=["insufficient_evidence"],
                retained=[False],
                forbidden=[bad],
            )
        )

    # Temporal variants
    f.append(
        syn(
            "SYN-036",
            category="future_effective",
            jurisdiction="NL",
            pathway_id="nl_highly_skilled_migrant",
            question="Current criterion?",
            evidence=[
                ev(
                    "Coming into force on 1 July 2028, the HSM criterion will be €3,500.",
                    "https://example.test/nl/hsm-future",
                    "NL",
                )
            ],
            claims=[claim("The highly skilled migrant criterion is currently €3,500.")],
            expected=["temporal_uncertainty"],
            retained=[True],
        )
    )
    f.append(
        syn(
            "SYN-037",
            category="historical_as_current",
            jurisdiction="NL",
            pathway_id="nl_highly_skilled_migrant",
            question="Current?",
            evidence=[
                ev(
                    "Formerly the HSM criterion was €2,400. That figure was superseded.",
                    "https://example.test/nl/hsm-old",
                    "NL",
                )
            ],
            claims=[claim("The highly skilled migrant criterion is €2,400.")],
            expected=["temporal_uncertainty"],
            retained=[True],
        )
    )

    # IE GEP related context without comparison should mismatch for CSEP pathway
    f.append(
        syn(
            "SYN-038",
            category="wrong_pathway_attribution",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Critical Skills?",
            evidence=[
                ev(
                    "General Employment Permit remuneration criteria. "
                    "Minimum annual remuneration of €36,605.",
                    "https://example.test/ie/general-employment-permit",
                    "IE",
                )
            ],
            claims=[
                claim(
                    "The General Employment Permit has a minimum annual remuneration "
                    "of €36,605."
                )
            ],
            expected=["pathway_mismatch"],
            retained=[False],
            forbidden=["€36,605"],
        )
    )

    # Correct CSEP figure on CSEP URL
    f.append(
        syn(
            "SYN-039",
            category="correct_salary",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Critical Skills remuneration?",
            evidence=[
                ev(
                    "Critical Skills Employment Permit criteria include occupation type "
                    "and proposed remuneration. Remuneration must be at least €38,000 "
                    "for some Critical Skills occupations.",
                    "https://example.test/ie/critical-skills-employment-permit",
                    "IE",
                )
            ],
            claims=[
                claim(
                    "Some Critical Skills Employment Permit occupations require "
                    "remuneration of at least €38,000."
                )
            ],
            expected=["supported"],
            required=["€38,000"],
        )
    )

    # Fabricated fee series
    for i, fee in enumerate(["£777", "€888", "£1,111"], start=40):
        f.append(
            syn(
                f"SYN-{i:03d}",
                category="fabricated_fee",
                jurisdiction="GB",
                pathway_id="gb_student",
                question="Fee?",
                evidence=[
                    ev(
                        "Student visa overview without a fee table in this excerpt.",
                        f"https://example.test/gb/student-{i}",
                    )
                ],
                claims=[claim(f"The Student visa application fee is exactly {fee}.")],
                expected=["insufficient_evidence"],
                retained=[False],
                forbidden=[fee],
            )
        )

    # Mixed answer with gap leak attempt
    f.append(
        syn(
            "SYN-043",
            category="gap_prose_leak",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Salary?",
            evidence=[
                ev(
                    "Alpha Visa general salary threshold is £41,700.",
                    "https://example.test/gb/alpha-visa",
                )
            ],
            claims=[
                claim("The Alpha Visa general salary threshold is £41,700."),
                claim("The Alpha Visa also requires a secret £55,555 deposit."),
            ],
            expected=["supported", "insufficient_evidence"],
            retained=[True, False],
            missing=["Whether a £55,555 deposit is required"],
            required=["£41,700"],
            forbidden=["£55,555", "55,555"],
            raw_answer=(
                "The Alpha Visa general salary threshold is £41,700. "
                "The Alpha Visa also requires a secret £55,555 deposit."
            ),
        )
    )

    # Unrelated correct + unsupported
    f.append(
        syn(
            "SYN-044",
            category="mixed_supported_unsupported",
            jurisdiction="GB",
            pathway_id="gb_student",
            question="Student work and fees?",
            evidence=[
                ev(
                    "Student permission: term-time work limited to 20 hours per week "
                    "for degree-level courses.",
                    "https://example.test/gb/student",
                )
            ],
            claims=[
                claim(
                    "Student permission generally limits term-time work to 20 hours "
                    "per week for degree-level courses."
                ),
                claim("The Student visa fee is £2,222."),
            ],
            expected=["supported", "insufficient_evidence"],
            retained=[True, False],
            forbidden=["£2,222"],
        )
    )

    # Incomplete conditions qualitative
    f.append(
        syn(
            "SYN-045",
            category="incomplete_evidence",
            jurisdiction="CA",
            pathway_id=None,
            question="Express Entry CRS cutoff?",
            evidence=[
                ev(
                    "Express Entry overview. Round-specific CRS cut-offs are not "
                    "listed in this excerpt.",
                    "https://example.test/ca/ee",
                    "CA",
                )
            ],
            claims=[claim("The current Express Entry CRS cutoff is exactly 500.")],
            expected=["insufficient_evidence"],
            retained=[False],
            forbidden=["500"],
        )
    )

    # --- Real official-style captured fixtures (thresholds not asserted as current law) ---
    # Qualitative / structural expectations only; reference_status unverified.
    f.append(
        real(
            "REAL-001",
            category="work_rights",
            jurisdiction="GB",
            pathway_id="gb_student",
            question="Student work conditions?",
            evidence=[
                ev(
                    "You can work up to 20 hours per week during term time if you’re "
                    "studying a course at degree level or above.",
                    "https://www.gov.uk/student-visa",
                    external=True,
                )
            ],
            claims=[
                claim(
                    "You can work up to 20 hours per week during term time if you’re "
                    "studying a course at degree level or above."
                )
            ],
            expected=["supported"],
            notes="Captured-style GOV.UK wording for structural test; not a legal currency check.",
        )
    )
    f.append(
        real(
            "REAL-002",
            category="wrong_pathway_attribution",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Critical Skills remuneration (F07-style)?",
            evidence=[
                ev(
                    "General Employment Permit remuneration criteria. "
                    "Minimum annual remuneration of €68,911 for eligible employment "
                    "where the job is not on the Critical Skills Occupations List.",
                    "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/permit-types/general-employment-permit/",
                    "IE",
                    external=True,
                    doc=501,
                ),
                ev(
                    "Critical Skills Employment Permit eligibility is largely determined "
                    "by the type of occupation and the proposed remuneration level.",
                    "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/permit-types/critical-skills-employment-permit/",
                    "IE",
                    external=True,
                    doc=502,
                    chunk=2,
                ),
            ],
            claims=[
                claim(
                    "The Critical Skills Employment Permit minimum annual "
                    "remuneration is €68,911.",
                    [0, 1],
                )
            ],
            expected=["pathway_mismatch"],
            retained=[False],
            forbidden=["€68,911"],
            notes="F07/X05 regression shape using official URL hosts; figure not certified current.",
        )
    )
    f.append(
        real(
            "REAL-003",
            category="valid_pathway_comparison",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Compare permits (X06-style)?",
            evidence=[
                ev(
                    "General Employment Permit remuneration criteria include thresholds "
                    "of €32,691. Critical Skills Employment Permit is a different permit type.",
                    "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/permit-types/general-employment-permit/",
                    "IE",
                    external=True,
                )
            ],
            claims=[
                claim(
                    "Unlike the Critical Skills Employment Permit, the General "
                    "Employment Permit has a separate remuneration threshold of €32,691."
                )
            ],
            expected=["supported"],
            notes="Comparison language must remain allowed.",
        )
    )
    f.append(
        real(
            "REAL-004",
            category="qualified_uncertainty",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Skilled Worker salary depends on what?",
            evidence=[
                ev(
                    "You’ll need to be paid a minimum salary. How much depends on "
                    "when you applied and the type of work.",
                    "https://www.gov.uk/skilled-worker-visa",
                    external=True,
                )
            ],
            claims=[
                claim(
                    "You’ll need to be paid a minimum salary; how much depends on "
                    "when you applied and the type of work."
                )
            ],
            expected=["supported"],
            notes="Qualitative GOV.UK-style claim without asserting a numeric threshold as current law.",
        )
    )
    f.append(
        real(
            "REAL-005",
            category="incomplete_evidence",
            jurisdiction="FR",
            pathway_id="fr_passeport_talent",
            question="All Talent categories?",
            evidence=[
                ev(
                    "Carte talent — titre de séjour pluriannuelle. "
                    "Detailed category conditions are not in this excerpt.",
                    "https://www.service-public.fr/particuliers/vosdroits/F16922",
                    "FR",
                    external=True,
                )
            ],
            claims=[
                claim(
                    "The carte talent has a fixed list of 15 categories each costing €250."
                )
            ],
            expected=["insufficient_evidence"],
            retained=[False],
            forbidden=["€250"],
        )
    )
    f.append(
        real(
            "REAL-006",
            category="unpublished",
            jurisdiction="FR",
            pathway_id=None,
            question="Unpublished scoring notes?",
            evidence=[
                ev(
                    "Autorisation de travail d'un salarié étranger en France. "
                    "No internal scoring notes are published on this page.",
                    "https://www.service-public.fr/particuliers/vosdroits/F2728",
                    "FR",
                    external=True,
                )
            ],
            claims=[
                claim(
                    "French salarié applications use unpublished Home Office scoring "
                    "notes with a mandatory 80-point pass mark."
                )
            ],
            expected=["insufficient_evidence"],
            retained=[False],
            forbidden=["80-point", "80"],
            notes="S05/X13-style trap.",
        )
    )
    f.append(
        real(
            "REAL-007",
            category="wrong_pathway_attribution",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Critical Skills (X05-style)?",
            evidence=[
                ev(
                    "General Employment Permit page lists minimum annual remuneration "
                    "of €68,911 for eligible employment.",
                    "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/permit-types/general-employment-permit/",
                    "IE",
                    external=True,
                )
            ],
            claims=[
                claim(
                    "DETE sets the Critical Skills Employment Permit salary at €68,911."
                )
            ],
            expected=["pathway_mismatch"],
            retained=[False],
            forbidden=["€68,911"],
        )
    )
    f.append(
        real(
            "REAL-008",
            category="correct_salary",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Critical Skills occupations remuneration theme?",
            evidence=[
                ev(
                    "Critical Skills Employment Permit. Remuneration must be at least "
                    "€38,000 for some Critical Skills occupations listed on the "
                    "Critical Skills Occupations List.",
                    "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/permit-types/critical-skills-employment-permit/",
                    "IE",
                    external=True,
                )
            ],
            claims=[
                claim(
                    "Some Critical Skills Employment Permit occupations require "
                    "remuneration of at least €38,000."
                )
            ],
            expected=["supported"],
            notes="Structural support test; figure not independently human-verified as current.",
            reference_status="machine_captured_unverified",
        )
    )
    f.append(
        real(
            "REAL-009",
            category="future_effective",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Future threshold presented as current?",
            evidence=[
                ev(
                    "From 1 April 2027, illustrative guidance states a general salary "
                    "threshold of £50,000 will apply. This is future-effective language "
                    "for testing only.",
                    "https://www.gov.uk/skilled-worker-visa",
                    external=True,
                )
            ],
            claims=[
                claim(
                    "The current Skilled Worker general salary threshold is £50,000."
                )
            ],
            expected=["temporal_uncertainty"],
            retained=[True],
            notes="Synthetic future date on official host for temporal behaviour; not a real announcement assertion.",
        )
    )
    f.append(
        real(
            "REAL-010",
            category="gap_prose_leak",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Do not leak GEP figure into gaps",
            evidence=[
                ev(
                    "Critical Skills Employment Permit eligibility depends on occupation "
                    "and remuneration; numeric bands are not shown in this excerpt.",
                    "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/permit-types/critical-skills-employment-permit/",
                    "IE",
                    external=True,
                    doc=601,
                ),
                ev(
                    "General Employment Permit minimum annual remuneration of €68,911.",
                    "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/permit-types/general-employment-permit/",
                    "IE",
                    external=True,
                    doc=602,
                    chunk=2,
                ),
            ],
            claims=[
                claim(
                    "The Critical Skills Employment Permit minimum annual remuneration "
                    "is €68,911.",
                    [0, 1],
                )
            ],
            expected=["pathway_mismatch"],
            retained=[False],
            missing=[
                "Whether €68,911 is the official Critical Skills threshold"
            ],
            forbidden=["€68,911", "68,911"],
        )
    )

    # Additional synthetic edge cases to reach ~70
    f.append(
        syn(
            "SYN-046",
            category="contradictory_passages",
            jurisdiction="GB",
            pathway_id="gb_student",
            question="Maintenance?",
            evidence=[
                ev(
                    "Maintenance outside London is £1,136 per month.",
                    "https://example.test/gb/student-a",
                    doc=701,
                ),
                ev(
                    "Maintenance outside London is exactly £1,023 per month only; "
                    "£1,136 does not apply.",
                    "https://example.test/gb/student-b",
                    doc=702,
                    chunk=2,
                ),
            ],
            claims=[
                claim(
                    "Maintenance outside London is only exactly £1,136 per month.",
                    [0, 1],
                )
            ],
            expected=["contradicted"],
            retained=[False],
        )
    )
    f.append(
        syn(
            "SYN-047",
            category="eligibility_exception",
            jurisdiction="NL",
            pathway_id="nl_highly_skilled_migrant",
            question="Age categories?",
            evidence=[
                ev(
                    "Highly skilled migrant salary criteria differ by age: under 30 "
                    "and 30 or older have separate monthly amounts.",
                    "https://example.test/nl/hsm",
                    "NL",
                )
            ],
            claims=[
                claim(
                    "Highly skilled migrant salary criteria differ by age, with "
                    "separate amounts for under 30 and 30 or older."
                )
            ],
            expected=["supported"],
        )
    )
    f.append(
        syn(
            "SYN-048",
            category="missing_material_conditions",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Eligibility?",
            evidence=[
                ev(
                    "Alpha Visa requires sponsorship, eligible occupation, English "
                    "language, and salary threshold.",
                    "https://example.test/gb/alpha",
                )
            ],
            claims=[
                claim(
                    "Meeting the salary threshold alone is enough for Alpha Visa approval."
                )
            ],
            expected=["insufficient_evidence"],
            retained=[False],
        )
    )
    f.append(
        syn(
            "SYN-049",
            category="qualified_uncertainty",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            question="Exact threshold?",
            evidence=[
                ev(
                    "Critical Skills Employment Permit eligibility depends on occupation "
                    "and proposed remuneration; specific bands are not listed here.",
                    "https://example.test/ie/critical-skills-employment-permit",
                    "IE",
                )
            ],
            claims=[
                claim(
                    "Critical Skills Employment Permit eligibility depends on occupation "
                    "and proposed remuneration; specific numeric bands are not listed "
                    "in the retrieved excerpt."
                )
            ],
            expected=["supported"],
        )
    )
    f.append(
        syn(
            "SYN-050",
            category="unrelated_correct",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            question="Salary and photos?",
            evidence=[
                ev(
                    "Alpha Visa salary threshold is £41,700. Biometric residence permits "
                    "are issued after a successful application.",
                    "https://example.test/gb/alpha",
                )
            ],
            claims=[
                claim("The Alpha Visa salary threshold is £41,700."),
                claim(
                    "Biometric residence permits are issued after a successful application."
                ),
            ],
            expected=["supported", "supported"],
        )
    )

    # Ensure unique IDs
    ids = [x["id"] for x in f]
    assert len(ids) == len(set(ids)), "duplicate fixture ids"

    return {
        "suite_id": "verification_reliability_9d3",
        "version": "1.0",
        "notes": [
            "Synthetic fixtures use stipulated rules; they do not assert real-world law.",
            "real_official fixtures use official hosts/captured-style wording for structural tests.",
            "Figures in real_official fixtures are not independently human-verified as current.",
            "Do not tune against held-out X16–X20.",
        ],
        "fixtures": f,
    }


def main() -> None:
    suite = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(suite, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    kinds = {}
    for fx in suite["fixtures"]:
        kinds[fx["kind"]] = kinds.get(fx["kind"], 0) + 1
    print(f"wrote {OUT} n={len(suite['fixtures'])} kinds={kinds}")


if __name__ == "__main__":
    main()
