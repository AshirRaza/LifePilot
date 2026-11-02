"""Independent Express Entry program assessment (FSW / CEC / FST) from stored IRCC pages.

Each program is assessed only from (a) its own program page and (b) shared IRCC pages
that state program-specific rules. Findings are never copied between programs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifepilot.coverage.source_expansion.relevance import (
    ExtractionRecord,
    block_text,
    load_current_extractions,
    url_key,
)

CA_SOURCE_ID = 9083
PROGRAMS: dict[str, dict[str, str]] = {
    "ca_federal_skilled_worker": {"page": "fsw", "name": "Federal Skilled Worker Program"},
    "ca_canadian_experience_class": {"page": "cec", "name": "Canadian Experience Class"},
    "ca_federal_skilled_trades": {"page": "fst", "name": "Federal Skilled Trades Program"},
}

# key -> URL suffix token (matched with endswith on host+path, lowercase)
DOC_TOKENS: dict[str, str] = {
    "fsw": "express-entry/who-can-apply/federal-skilled-workers.html",
    "cec": "express-entry/who-can-apply/canadian-experience-class.html",
    "fst": "express-entry/who-can-apply/federal-skilled-trades.html",
    "language": "express-entry/documents/language-test.html",
    "job_offer": "express-entry/documents/job-offer.html",
    "eca": "express-entry/documents/education-assessment.html",
    "pof": "express-entry/documents/proof-funds.html",
    "police": "express-entry/documents/police-certificates.html",
    "docs_hub": "express-entry/documents.html",
    "create_profile": "express-entry/create-profile.html",
    "rounds": "express-entry/rounds-invitations.html",
    "apply_pr": "express-entry/apply-permanent-residence.html",
}

REQUIREMENTS = (
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
)


def load_canada_records(session: Session) -> dict[str, ExtractionRecord]:
    rows = session.execute(
        text(
            "SELECT id, canonical_url FROM documents "
            "WHERE primary_source_id = :sid ORDER BY id"
        ),
        {"sid": CA_SOURCE_ID},
    ).mappings().all()
    wanted: dict[str, int] = {}
    for r in rows:
        key = url_key(r["canonical_url"])
        for dk, token in DOC_TOKENS.items():
            if key.endswith(token):
                wanted[dk] = int(r["id"])
    recs = load_current_extractions(session, list(wanted.values()))
    return {dk: recs[did] for dk, did in wanted.items() if did in recs}


@dataclass
class Hit:
    doc_key: str
    document_id: int
    version_id: int
    block: int
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc": self.doc_key,
            "document_id": self.document_id,
            "version_id": self.version_id,
            "block": self.block,
            "snippet": self.snippet,
        }


def _subtree_range(blocks: list[dict[str, Any]], heading_re: str) -> tuple[int, int] | None:
    pat = re.compile(heading_re, re.I)
    for i, b in enumerate(blocks):
        if b.get("type") == "heading" and pat.search(str(b.get("text") or "")):
            level = int(b.get("level") or 2)
            end = len(blocks) - 1
            for j in range(i + 1, len(blocks)):
                bj = blocks[j]
                if bj.get("type") == "heading" and int(bj.get("level") or 2) <= level:
                    end = j - 1
                    break
            return i, end
    return None


def _find(
    records: dict[str, ExtractionRecord],
    doc_key: str,
    regex: str,
    *,
    subtree: str | None = None,
    window: int = 1,
) -> Hit | None:
    rec = records.get(doc_key)
    if rec is None:
        return None
    lo, hi = 0, len(rec.blocks) - 1
    if subtree:
        rng = _subtree_range(rec.blocks, subtree)
        if rng is None:
            return None
        lo, hi = rng
    pat = re.compile(regex, re.I | re.S)
    for i in range(lo, hi + 1):
        txt = " ".join(block_text(rec.blocks[j]) for j in range(i, min(i + window, hi + 1)))
        m = pat.search(txt)
        if m:
            start = max(0, m.start() - 40)
            snippet = txt[start : m.end() + 80].strip()
            return Hit(doc_key, rec.document_id, rec.version_id, i, snippet[:260])
    return None


# subrequirement name -> (doc_key, regex, optional subtree heading regex)
Check = tuple[str, str, str, str | None]


def _eligibility_checks(program_id: str) -> list[Check]:
    page = PROGRAMS[program_id]["page"]
    common = [
        ("admissibility", page, r"must be admissible to Canada", None),
        ("live_outside_quebec", page, r"outside the province of Quebec", None),
        ("language_tests_all_4_abilities", page, r"approved language tests in English or French.*?all 4 abilities|minimum score in all 4 abilities", None),
    ]
    if program_id == "ca_federal_skilled_worker":
        return [
            ("work_experience_1_year_1560_hours", "fsw", r"1 year of continuous work or 1,?560 hours", None),
            ("work_experience_within_10_years", "fsw", r"obtained within the last 10 years", None),
            ("teer_0_1_2_3", "fsw", r"TEER categories: 0, 1, 2, or 3", None),
            ("selection_points_67", "fsw", r"67 points or higher", None),
            ("education_eca_for_foreign_credential", "fsw", r"Educational Credential Assessment", None),
            ("eca_required_for_fsw_principal", "eca", r"eligible as the principal applicant for Federal Skilled Worker", None),
            ("language_clb_minimum_fsw", "language", r"English.{0,6}CLB 7", r"^Federal Skilled Worker Program$"),
            ("job_offer_not_required_proof_of_funds_instead", "job_offer", r"don.t have a valid job offer, you.ll need to provide proof of funds", r"^Federal Skilled Workers Program$"),
            *common,
        ]
    if program_id == "ca_canadian_experience_class":
        return [
            ("canadian_work_experience", "cec", r"gained by working in Canada", None),
            ("work_experience_1_year_1560_hours", "cec", r"1 year of work or 1,?560 hours total", None),
            ("work_experience_within_3_years", "cec", r"in the 3 years before you apply", None),
            ("education_not_required", "cec", r"no education requirement for the Canadian Experience Class", None),
            ("language_clb_minimum_cec", "language", r"TEER 0 or 1.*?CLB 7.*?TEER 2 or 3.*?CLB 5", r"^Canadian Experience Class$"),
            ("job_offer_not_required", "job_offer", r"don.t need a job offer to be eligible", r"^Canadian Experience Class$"),
            *common,
        ]
    return [
        ("work_experience_2_years_3120_hours", "fst", r"2 years of full-time work experience \(or 3,?120 hours", None),
        ("work_experience_within_5_years", "fst", r"within the 5 years before you apply", None),
        ("trade_noc_groups", "fst", r"Major Groups? 72", None),
        ("job_offer_or_certificate_of_qualification", "fst", r"valid job offer of full-time employment.{0,200}certificate of qualification", None),
        ("education_not_required", "fst", r"no education requirement for the Federal Skilled Trades Program", None),
        ("language_clb_minimum_fst", "language", r"Speaking and listening.{0,40}CLB 5.{0,200}Reading and writing.{0,40}CLB 4", r"^Federal Skilled Trades Program$"),
        ("job_offer_requirements_for_fst", "job_offer", r"If you don.t have a valid job offer, you.ll need a certificate of qualification", r"^Federal Skilled Trades Program$"),
        *common,
    ]


def _run_checks(
    records: dict[str, ExtractionRecord], checks: list[Check]
) -> tuple[list[tuple[str, Hit]], list[str]]:
    found: list[tuple[str, Hit]] = []
    missing: list[str] = []
    for name, dk, rx, sub in checks:
        hit = _find(records, dk, rx, subtree=sub, window=2)
        if hit is None:
            missing.append(name)
        else:
            found.append((name, hit))
    return found, missing


def _row(
    *,
    program_id: str,
    requirement: str,
    classification: str,
    found: list[tuple[str, Hit]],
    missing: list[str],
    rationale: str,
    assessed_at: str,
    primary_key: str,
    records: dict[str, ExtractionRecord],
    shared_evidence: bool = False,
    conditional: str = "",
    facts: dict[str, Any] | None = None,
    notes: str = "",
    proposed_missing: str = "",
) -> dict[str, Any]:
    prim = records.get(primary_key)
    spans = [h.to_dict() | {"subrequirement": n} for n, h in found]
    doc_ids = sorted({h.document_id for _, h in found} | ({prim.document_id} if prim else set()))
    passage = " || ".join(f"[{n}] {h.snippet}" for n, h in found)[:700]
    return {
        "pathway_id": program_id,
        "program_name": PROGRAMS[program_id]["name"],
        "country": "CA",
        "requirement": requirement,
        "classification": classification,
        "document_id": prim.document_id if prim else "",
        "version_id": prim.version_id if prim else "",
        "supporting_document_ids": json.dumps(doc_ids),
        "source_url": prim.source_url if prim else "",
        "evidence_spans": json.dumps(spans, ensure_ascii=False),
        "passage": passage,
        "matched_subrequirements": ",".join(n for n, _ in found),
        "missing_subrequirements": ",".join(missing),
        "rationale": rationale,
        "shared_evidence": shared_evidence,
        "conditional_applicability": conditional,
        "extracted_facts": json.dumps(facts or {}, ensure_ascii=False),
        "source_notes": notes,
        "proposed_missing_url": proposed_missing,
        "assessed_at": assessed_at,
        "reviewer_status": "automated_provisional",
    }


PROGRAM_FACTS: dict[str, dict[str, Any]] = {
    "ca_federal_skilled_worker": {
        "min_work_experience": "1 year continuous / 1,560 hours, within last 10 years, TEER 0-3, Canada or abroad",
        "language_minimum": "CLB 7 all 4 abilities (first official language)",
        "education": "Required (Canadian credential or foreign credential + ECA); 67-point selection grid",
        "job_offer": "Not required for eligibility (arranged-employment points only); needed to waive proof of funds",
        "proof_of_funds": "Required unless authorized to work in Canada with a valid job offer",
    },
    "ca_canadian_experience_class": {
        "min_work_experience": "1 year / 1,560 hours of Canadian skilled work in the 3 years before applying, TEER 0-3",
        "language_minimum": "CLB 7 (TEER 0/1) or CLB 5 (TEER 2/3), all 4 abilities",
        "education": "No education requirement (points only)",
        "job_offer": "Not required for eligibility",
        "proof_of_funds": "Not required (exempt) under CEC",
    },
    "ca_federal_skilled_trades": {
        "min_work_experience": "2 years full-time / 3,120 hours in a skilled trade within the 5 years before applying",
        "language_minimum": "CLB 5 speaking/listening and CLB 4 reading/writing",
        "education": "No education requirement (points only)",
        "job_offer": "Valid job offer (1 yr) OR certificate of qualification required",
        "proof_of_funds": "Required unless authorized to work in Canada with a valid job offer",
    },
}


def assess_canada(
    records: dict[str, ExtractionRecord],
    *,
    assessed_at: str,
    source_approved: bool = True,
) -> list[dict[str, Any]]:
    """Return 15 evidence rows (3 programs x 5 critical requirements)."""
    rows: list[dict[str, Any]] = []
    shared_process_checks: list[Check] = [
        ("create_profile_and_enter_pool", "create_profile", r"create your profile and submit it to the Express Entry pool", None),
        ("invitation_to_apply", "create_profile", r"invite you to apply", None),
        ("apply_steps_form_upload_pay_submit", "apply_pr", r"Fill out the application form.*Upload your documents.*Pay the fees.*Submit your application", None),
        ("invitation_valid_60_days", "apply_pr", r"invitation to apply is valid for 60 days", None),
    ]
    for program_id, meta in PROGRAMS.items():
        page = meta["page"]
        facts = PROGRAM_FACTS[program_id]

        # ---- eligibility
        found, missing = _run_checks(records, _eligibility_checks(program_id))
        if page not in records:
            cls = "MISSING"
            why = f"Program page ({page}) not in stored corpus; propose ingesting the exact IRCC program page."
        elif missing:
            cls = "PARTIAL"
            why = "Program page present but sub-requirements not located: " + ", ".join(missing)
        else:
            cls = "COMPLETE"
            why = (
                f"All {len(found)} program-specific eligibility sub-requirements located in "
                f"the {meta['name']} page and IRCC language/job-offer/ECA pages (not inherited "
                "from the other programs)."
            )
        notes = ""
        fst = records.get("fst")
        if program_id == "ca_federal_skilled_trades" and fst is not None:
            if _find(records, "fst", r"adds up to 1,?560 hours"):
                notes = (
                    "Source inconsistency: FST page states 3,120 hours total but its part-time "
                    "paragraph says 'adds up to 1,560 hours'; 3,120 hours (2 years full-time) "
                    "used as stated in the minimum-requirements list. Flag for human review."
                )
        rows.append(
            _row(
                program_id=program_id, requirement="eligibility", classification=cls,
                found=found, missing=missing, rationale=why, assessed_at=assessed_at,
                primary_key=page, records=records, facts=facts, notes=notes,
            )
        )

        # ---- required documents (never COMPLETE without the post-invitation checklist)
        doc_checks: list[Check] = [
            ("hub_lists_language_results", "docs_hub", r"language test results", None),
            ("hub_lists_work_experience_proof", "docs_hub", r"proof of work experience", None),
            ("hub_lists_police_certificates", "docs_hub", r"police certificates", None),
            ("hub_lists_proof_of_funds", "docs_hub", r"proof of funds", None),
        ]
        if program_id == "ca_federal_skilled_worker":
            doc_checks.append(("eca_report_for_foreign_education", "eca", r"eligible as the principal applicant for Federal Skilled Worker", None))
        elif program_id == "ca_federal_skilled_trades":
            doc_checks.append(("certificate_of_qualification_or_job_offer_listed", "docs_hub", r"certificate of qualification in a trade", None))
        else:
            doc_checks.append(("no_education_document_requirement", "cec", r"no education requirement for the Canadian Experience Class", None))
        found, missing = _run_checks(records, doc_checks)
        missing_all = missing + ["post_invitation_pr_application_document_checklist"]
        rows.append(
            _row(
                program_id=program_id, requirement="required_documents",
                classification="PARTIAL" if "docs_hub" in records else "MISSING",
                found=found, missing=missing_all,
                rationale=(
                    "Express Entry documents hub lists documents needed to create a profile "
                    "(language, ECA, work proof, funds, police) with program-conditional items; "
                    "it is a shared hub and not the post-invitation PR application checklist, so "
                    "the mandatory per-program document list is not established."
                ),
                assessed_at=assessed_at, primary_key=page, records=records,
                shared_evidence=True, facts={"document_hub_scope": "profile stage; shared across programs"},
                proposed_missing=(
                    "research required: exact IRCC post-invitation PR document checklist page "
                    "(no URL asserted)"
                ),
            )
        )

        # ---- application process (shared official system process + program gate)
        found, missing = _run_checks(records, shared_process_checks)
        gate = _find(records, page, r"profile")
        if gate:
            found.append(("program_page_references_profile_step", gate))
        else:
            missing.append("program_page_references_profile_step")
        cls = "COMPLETE" if not missing and page in records else ("PARTIAL" if found else "MISSING")
        rows.append(
            _row(
                program_id=program_id, requirement="application_process", classification=cls,
                found=found, missing=missing,
                rationale=(
                    "Process steps (profile, pool, invitation, upload/pay/submit) are stated by "
                    "IRCC once for all Express Entry programs; marked shared evidence and gated "
                    "by this program's own page."
                ),
                assessed_at=assessed_at, primary_key=page, records=records, shared_evidence=True,
            )
        )

        # ---- financial / proof of funds
        if program_id == "ca_canadian_experience_class":
            found, missing = _run_checks(
                records,
                [
                    ("pof_exempt_under_cec", "pof", r"applying under the Canadian Experience Class", r"Who doesn.t need proof of funds"),
                ],
            )
            cls = "NOT_APPLICABLE" if not missing else "PARTIAL"
            why = (
                "IRCC proof-of-funds page lists applying under the Canadian Experience Class "
                "as not needing proof of funds (system still asks for an explanatory letter)."
                if not missing
                else "Exemption statement not located in stored proof-of-funds page."
            )
            rows.append(
                _row(
                    program_id=program_id, requirement="financial_or_sponsorship",
                    classification=cls, found=found, missing=missing, rationale=why,
                    assessed_at=assessed_at, primary_key="pof", records=records,
                    shared_evidence=True,
                    conditional="CEC exempt; upload letter stating CEC invitation if system requests a document",
                    facts={"proof_of_funds": facts["proof_of_funds"]},
                )
            )
        else:
            prog_regex = (
                r"Federal Skilled Worker Program"
                if program_id == "ca_federal_skilled_worker"
                else r"Federal Skilled Trades Program"
            )
            checks_f: list[Check] = [
                ("pof_required_for_program", "pof", r"minimum requirements of the:.*" + prog_regex, r"^Who needs proof of funds$"),
                ("pof_amount_table_by_family_size", "pof", r"Funds you need \(CAD\).*?\$\d", None),
                ("pof_exemption_authorized_with_job_offer", "pof", r"authorized to work in Canada and you have a valid job offer", None),
                ("pof_bank_letter_requirements", "pof", r"You need a bank letter|official letters from any banks", None),
                ("program_page_requires_funds", page, r"must show that you have enough money for you and your family", None),
            ]
            found, missing = _run_checks(records, checks_f)
            cls = "COMPLETE" if not missing and page in records else "PARTIAL"
            rows.append(
                _row(
                    program_id=program_id, requirement="financial_or_sponsorship",
                    classification=cls, found=found, missing=missing,
                    rationale=(
                        "Applicability to this program, family-size amounts, exemption conditions "
                        "and accepted evidence located; amounts are IRCC-updated figures "
                        "(re-verify currency before use)."
                        if cls == "COMPLETE"
                        else "Proof-of-funds sub-requirements missing: " + ", ".join(missing)
                    ),
                    assessed_at=assessed_at, primary_key="pof", records=records,
                    shared_evidence=True,
                    conditional="exempt when authorized to work in Canada with a valid job offer",
                    facts={"proof_of_funds": facts["proof_of_funds"]},
                )
            )

        # ---- provenance
        prim = records.get(page)
        prov_cls = "COMPLETE" if prim is not None and source_approved else "PARTIAL"
        found = (
            [("approved_source_9083_program_page", Hit(page, prim.document_id, prim.version_id, 0, prim.source_url))]
            if prim
            else []
        )
        rows.append(
            _row(
                program_id=program_id, requirement="official_provenance",
                classification=prov_cls, found=found, missing=[] if prim else ["program_page_not_ingested"],
                rationale="IRCC canada.ca approved source 9083; program page ingested and extracted.",
                assessed_at=assessed_at, primary_key=page, records=records,
            )
        )
    return rows


def program_differences(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact comparison used in the report (facts, not inherited classifications)."""
    out: dict[str, Any] = {"facts": PROGRAM_FACTS, "classifications": {}}
    for r in rows:
        out["classifications"].setdefault(r["pathway_id"], {})[r["requirement"]] = r[
            "classification"
        ]
    return out
