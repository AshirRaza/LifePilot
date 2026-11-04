"""Cross-jurisdiction guards and machine-upgrade revalidation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lifepilot.coverage.corpus_reliability.assessment import get_classification
from lifepilot.coverage.corpus_reliability.matrix_io import (
    BLOCKED_RECOVERIES_NAME,
    is_eu_framework_url,
    load_csv,
)
from lifepilot.coverage.corpus_reliability.pathway_applicability import (
    document_applies_to_pathway,
)
from lifepilot.coverage.corpus_reliability.requirement_support import (
    validate_evidence_claim,
)

COUNTRY_HOST_HINTS: dict[str, tuple[str, ...]] = {
    "AE": ("u.ae", "icp.gov.ae", "government.ae"),
    "AU": ("homeaffairs.gov.au", "immi.homeaffairs"),
    "CA": ("canada.ca", "cic.gc.ca"),
    "CH": ("sem.admin.ch", "admin.ch"),
    "DE": ("bamf.de", "make-it-in-germany.com", "auswaertiges-amt.de"),
    "FR": ("service-public.gouv.fr", "france-visas.gouv.fr", "immigration.interieur"),
    "GB": ("gov.uk",),
    "IE": ("enterprise.gov.ie", "irishimmigration.ie", "gov.ie"),
    "JP": ("moj.go.jp", "isa."),
    "NL": ("ind.nl",),
    "NZ": ("immigration.govt.nz", "govt.nz"),
    "SG": ("ica.gov.sg", "mom.gov.sg"),
    "US": ("uscis.gov", "travel.state.gov", "state.gov"),
}

KNOWN_CONTAMINATION: list[dict[str, str]] = [
    {
        "pathway_id": "ae_student",
        "requirement": "application_process",
        "url_substr": "ind.nl/en/residence-permits/study",
        "reason": (
            "Cross-jurisdiction contamination: UAE student pathway associated with "
            "Dutch IND student residence guidance."
        ),
    },
    {
        "pathway_id": "ae_student",
        "requirement": "financial_or_sponsorship",
        "url_substr": "ind.nl/en/residence-permits/study",
        "reason": (
            "Cross-jurisdiction contamination: UAE student pathway associated with "
            "Dutch IND student residence guidance."
        ),
    },
    {
        "pathway_id": "fr_naturalisation",
        "requirement": "eligibility",
        "url_substr": "ind.nl/en/dutch-citizenship",
        "reason": (
            "Cross-jurisdiction contamination: French naturalisation pathway associated "
            "with Dutch IND naturalisation guidance."
        ),
    },
    {
        "pathway_id": "fr_naturalisation",
        "requirement": "application_process",
        "url_substr": "ind.nl/en/dutch-citizenship",
        "reason": (
            "Cross-jurisdiction contamination: French naturalisation pathway associated "
            "with Dutch IND naturalisation guidance."
        ),
    },
    {
        "pathway_id": "nz_skilled_migrant",
        "requirement": "financial_or_sponsorship",
        "url_substr": "ind.nl/en/residence-permits/work/highly-skilled-migrant",
        "reason": (
            "Cross-pathway/jurisdiction contamination: NZ Skilled Migrant financial "
            "evidence cites Dutch IND highly skilled migrant sponsorship guidance."
        ),
    },
]


def host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def scan_cross_jurisdiction_contamination(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for row in rows:
        if row.get("entity_type") != "true_pathway":
            continue
        cls = get_classification(row)
        if cls not in ("PARTIAL", "COMPLETE"):
            continue
        url = (row.get("source_url") or "").strip()
        if not url:
            continue
        country = (row.get("country") or "").upper()
        hints = COUNTRY_HOST_HINTS.get(country, ())
        if not hints:
            continue
        url_l = url.lower()
        if any(h in url_l for h in hints):
            continue
        kind = "eu_framework" if is_eu_framework_url(url_l) else "cross_jurisdiction"
        findings.append({
            "pathway_id": row["pathway_id"],
            "requirement": row["requirement"],
            "country": country,
            "classification": cls,
            "document_id": row.get("document_id") or "",
            "source_url": url,
            "kind": kind,
            "passage": (row.get("passage") or "")[:160],
            "method": row.get("method") or "",
        })
    return findings


def load_blocked_recoveries(runs_dir: Path) -> set[tuple[str, str, str]]:
    blocked: set[tuple[str, str, str]] = set()
    for spec in KNOWN_CONTAMINATION:
        blocked.add((spec["pathway_id"], spec["requirement"], spec["url_substr"].lower()))
    inv_path = runs_dir / BLOCKED_RECOVERIES_NAME
    if inv_path.is_file():
        for r in load_csv(inv_path):
            blocked.add((
                r.get("pathway_id") or "",
                r.get("requirement") or "",
                (r.get("source_url") or "").lower(),
            ))
    return blocked


def revalidate_machine_upgrades(
    rows: list[dict[str, Any]],
    prior_changes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Re-check machine PARTIAL/COMPLETE claims with requirement support."""
    out = [dict(r) for r in rows]
    changes: list[dict[str, Any]] = []

    suspects = {
        (c["pathway_id"], c["requirement"])
        for c in prior_changes
        if c.get("action") in {"pathway_aware_upgrade", "pathway_aware_reassess"}
        or "machine" in str(c.get("method", ""))
    }

    for r in out:
        if r.get("entity_type") != "true_pathway":
            continue
        cls = get_classification(r)
        if cls not in ("PARTIAL", "COMPLETE"):
            continue
        key = (r["pathway_id"], r["requirement"])
        method = str(r.get("method") or r.get("evidence_repair") or "")
        is_suspect = (
            key in suspects
            or "pathway_aware" in method
            or "machine" in method
            or "phase_6j" in method
        )
        if not is_suspect and cls == "COMPLETE":
            support = validate_evidence_claim(
                pathway_id=r["pathway_id"],
                requirement=r["requirement"],
                passage=r.get("passage") or "",
                source_url=r.get("source_url") or "",
                classification=cls,
            )
            if support.ok or support.kind not in {"mismatch", "wrong_category", "exemption"}:
                continue
            is_suspect = True
        if not is_suspect:
            continue

        support = validate_evidence_claim(
            pathway_id=r["pathway_id"],
            requirement=r["requirement"],
            passage=r.get("passage") or "",
            source_url=r.get("source_url") or "",
            title=r.get("title") or "",
            classification=cls,
        )
        appl = document_applies_to_pathway(
            pathway_id=r["pathway_id"],
            source_url=r.get("source_url") or "",
            title=r.get("title") or "",
            body_text=r.get("passage") or "",
        )
        if appl.applies and support.ok:
            continue

        prev = cls
        reason = support.reason if not support.ok else appl.reason

        # CEC proof-of-funds misfiled under required_documents → clear docs cell and
        # record financial_or_sponsorship as justified NOT_APPLICABLE.
        if (
            r["pathway_id"] == "ca_canadian_experience_class"
            and r["requirement"] == "required_documents"
            and ("proof_of_funds" in reason or "proof of funds" in reason.lower())
        ):
            r["classification"] = "UNEVALUATED"
            r["document_id"] = ""
            r["version_id"] = ""
            r["passage"] = ""
            r["source_url"] = ""
            r["method"] = "machine_upgrade_revalidate"
            r["evidence_repair"] = "reject_pof_as_required_documents"
            changes.append({
                "pathway_id": r["pathway_id"],
                "requirement": r["requirement"],
                "prev_classification": prev,
                "new_classification": "UNEVALUATED",
                "rationale": reason,
                "method": "machine_upgrade_revalidate",
                "action": "reject_requirement_mismatch",
            })
            fin = next(
                (
                    x for x in out
                    if x.get("pathway_id") == "ca_canadian_experience_class"
                    and x.get("requirement") == "financial_or_sponsorship"
                    and x.get("entity_type") == "true_pathway"
                ),
                None,
            )
            if fin is not None:
                fin_prev = get_classification(fin)
                fin["classification"] = "NOT_APPLICABLE"
                fin["not_applicable_rationale"] = (
                    "Canadian Experience Class applicants do not need proof of funds "
                    "(official IRCC exemption)."
                )
                fin["explanation"] = fin["not_applicable_rationale"]
                fin["passage"] = (
                    "Who doesn’t need proof of funds — You don’t need proof of funds "
                    "if you’re applying under the Canadian Experience Class."
                )
                fin["method"] = "machine_upgrade_revalidate"
                changes.append({
                    "pathway_id": fin["pathway_id"],
                    "requirement": fin["requirement"],
                    "prev_classification": fin_prev,
                    "new_classification": "NOT_APPLICABLE",
                    "rationale": fin["not_applicable_rationale"],
                    "method": "machine_upgrade_revalidate",
                    "action": "record_exemption_not_applicable",
                })
            continue

        new_cls = "UNEVALUATED"
        r["classification"] = new_cls
        r["document_id"] = ""
        r["version_id"] = ""
        r["passage"] = ""
        r["source_url"] = ""
        r["method"] = "machine_upgrade_revalidate"
        r["evidence_repair"] = "reject_unsupported_machine_claim"
        changes.append({
            "pathway_id": r["pathway_id"],
            "requirement": r["requirement"],
            "prev_classification": prev,
            "new_classification": new_cls,
            "rationale": reason,
            "method": "machine_upgrade_revalidate",
            "action": "invalidate_unsupported_evidence",
        })
    return out, changes
