"""Pathway-aware evidence assessment for existing corpus documents.

Rules:
- Keyword match alone never upgrades PARTIAL/COMPLETE.
- Document must first apply to the pathway (URL tokens / official scope).
- Machine assessments are at most PARTIAL; never COMPLETE; never human_verified.
- Prefer UNEVALUATED when association is uncertain.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifepilot.coverage.corpus_reliability.pathway_applicability import (
    PATHWAY_URL_TOKENS,
    document_applies_to_pathway,
)
from lifepilot.coverage.corpus_reliability.requirement_support import (
    passage_supports_requirement,
    validate_evidence_claim,
)

PRIORITY_PATHWAYS = [
    "gb_student", "gb_skilled_worker", "gb_graduate",
    "de_eu_blue_card",
    "nl_highly_skilled_migrant", "nl_naturalisation", "nl_permanent_residence",
    "us_h1b", "us_l1", "us_o1", "us_e2", "us_f1",
    "au_temporary_graduate_485",
    "ca_federal_skilled_worker", "ca_canadian_experience_class", "ca_federal_skilled_trades",
    "nz_skilled_migrant", "nz_essential_skills", "nz_citizenship_by_grant",
    "sg_permanent_residence", "sg_employment_pass",
    "ie_critical_skills_employment_permit", "ie_general_employment_permit",
]

CRITICAL_REQUIREMENTS = [
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
]

# Requirement cues applied only inside pathway-applicable documents,
# preferably on heading-scoped sections.
REQUIREMENT_HEADING_PATTERNS: dict[str, re.Pattern[str]] = {
    "eligibility": re.compile(
        # Avoid bare "requirement(s)" — matches financial-requirement headings.
        r"eligib|who can|who may|qualif|criteria|conditions to|"
        r"minimum requirements|do you qualify|entry requirements|visa requirements|"
        r"remplissez les conditions|conditions de d[eé]livrance|motifs? suivants|"
        r"vous pouvez obtenir",
        re.I,
    ),
    "required_documents": re.compile(
        r"documents?\b|evidence|checklist|what you.?ll need|supporting|proof of|"
        r"what to (?:provide|bring|submit)|"
        r"documents? [aà] fournir|pi[eè]ces? [aà] fournir|pr[eé]parer les documents",
        re.I,
    ),
    "application_process": re.compile(
        r"how to apply|apply\b|application process|the process|steps|where to apply|"
        r"submit|procedure|applying|"
        r"d[eé]marches?|faire la d[eé]marche|d[eé]poser votre demande|"
        r"[eé]tapes? de la d[eé]marche",
        re.I,
    ),
    "financial_or_sponsorship": re.compile(
        r"fees?\b|costs?\b|how much|funds|income|salary|financ|sponsor|money|"
        r"remuneration|maintenance|savings|health surcharge|"
        r"r[eé]mun[eé]ration|seuil de salaire|ressources",
        re.I,
    ),
}

REQUIREMENT_BODY_PATTERNS: dict[str, list[str]] = {
    "eligibility": [
        r"eligib[a-z]*",
        r"who\s+can\s+apply",
        r"must\s+(be|have)",
        r"qualif[a-z]*",
        r"criteria",
        r"remplissez\s+les\s+conditions",
        r"conditions\s+de\s+d[eé]livrance",
        r"vous\s+pouvez\s+obtenir",
    ],
    "required_documents": [
        r"document[s]?\s+(required|needed|checklist)",
        r"supporting\s+document",
        r"you\s+(will\s+)?need\s+to\s+(provide|submit)",
        r"checklist",
        r"documents?\s+[aà]\s+fournir",
        r"pi[eè]ces?\s+[aà]\s+fournir",
        r"attestation\s+employeur",
    ],
    "application_process": [
        r"how\s+to\s+apply",
        r"application\s+(process|procedure|form)",
        r"step[s]?\s+to\s+apply",
        r"submit\s+(your\s+)?application",
        r"fill\s+out\s+the\s+application",
        r"apply\s+(?:online|for\s+permanent\s+residence)",
        r"d[eé]poser\s+votre\s+demande",
        r"faire\s+la\s+d[eé]marche",
        r"[eé]tapes?\s+de\s+la\s+d[eé]marche",
    ],
    "financial_or_sponsorship": [
        r"financial\s+requirement",
        r"proof\s+of\s+funds",
        r"sponsor(ship)?",
        r"salary\s+(threshold|requirement)",
        r"income\s+requirement",
        r"r[eé]mun[eé]ration",
        r"seuil\s+de\s+salaire",
    ],
}

MIN_PASSAGE_CHARS = 50
MIN_SECTION_WORDS = 40


@dataclass
class Assessment:
    """A single evidence assessment."""

    pathway_id: str
    requirement: str
    prev_classification: str
    new_classification: str
    document_id: int | None
    version_id: int | None
    source_url: str
    passage: str
    rationale: str
    method: str = "pathway_aware_machine"
    confidence: str = "medium"
    applicability_basis: str = ""
    assessed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def get_classification(row: dict[str, Any]) -> str:
    return row.get("classification") or row.get("classification_6h") or "UNEVALUATED"


def match_requirement(text: str, requirement: str) -> tuple[bool, str, str]:
    """Legacy helper: body pattern match. Not sufficient alone for upgrades."""
    patterns = REQUIREMENT_BODY_PATTERNS.get(requirement, [])
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            start = max(0, match.start() - 100)
            end = min(len(text), match.end() + 300)
            return True, pattern, text[start:end].strip()
    return False, "", ""


def _block_text(block: dict[str, Any]) -> str:
    t = block.get("type")
    if t in {"heading", "paragraph", "list_item", "table_cell"}:
        return str(block.get("text") or "")
    if t == "list":
        return " \u2022 ".join(str(i) for i in block.get("items") or [])
    return str(block.get("text") or "")


def _sections_from_blocks(blocks: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return (heading, body) sections from extraction blocks."""
    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_body: list[str] = []
    for block in blocks:
        btype = block.get("type")
        text = _block_text(block).strip()
        if not text:
            continue
        if btype == "heading":
            if current_heading or current_body:
                sections.append((current_heading, " ".join(current_body)))
            current_heading = text
            current_body = []
        else:
            current_body.append(text)
    if current_heading or current_body:
        sections.append((current_heading, " ".join(current_body)))
    return sections


def extract_requirement_passage(
    blocks: list[dict[str, Any]],
    requirement: str,
) -> tuple[bool, str, str]:
    """Find a supporting passage for a requirement inside an applicable document.

    Prefers heading-scoped sections with enough body words. Falls back to body
    patterns only within this already-applicable document.
    """
    heading_re = REQUIREMENT_HEADING_PATTERNS.get(requirement)
    body_patterns = [re.compile(p, re.I) for p in REQUIREMENT_BODY_PATTERNS.get(requirement, [])]
    sections = _sections_from_blocks(blocks)
    candidates: list[tuple[str, str, str]] = []
    for heading, body in sections:
        if heading_re and heading_re.search(heading):
            words = len(body.split())
            if words >= MIN_SECTION_WORDS and len(body) >= MIN_PASSAGE_CHARS:
                passage = f"{heading}. {body}".strip()
                candidates.append((f"heading:{heading[:80]}", passage[:500], body))
    # Prefer heading sections whose body also matches a requirement body cue
    # (avoids "financial requirement" / "not invited" false first-hits).
    scored: list[tuple[int, str, str]] = []
    for cue, passage, body in candidates:
        hits = sum(1 for p in body_patterns if p.search(body) or p.search(passage))
        # Prefer headings that themselves look like process steps / how-to.
        if re.search(r"how to apply|submit your application|application process|fill out", cue, re.I):
            hits += 2
        if hits:
            scored.append((hits, cue, passage))
    if scored:
        scored.sort(key=lambda x: -x[0])
        _, cue, passage = scored[0]
        return True, cue, passage
    if candidates:
        cue, passage, _ = candidates[0]
        return True, cue, passage
    full_text = " ".join(_block_text(b) for b in blocks)
    matched, pattern, passage = match_requirement(full_text, requirement)
    if matched and len(passage) >= MIN_PASSAGE_CHARS:
        return True, f"body:{pattern}", passage[:500]
    return False, "", ""


def assess_document_for_pathway(
    extraction_blocks: list[dict[str, Any]],
    pathway_id: str,
    requirement: str,
    document_id: int,
    version_id: int,
    source_url: str,
    *,
    title: str = "",
    official_url: str = "",
) -> Assessment | None:
    """Assess a document for a pathway/requirement with pathway-aware gating."""
    if requirement == "official_provenance":
        return None

    full_text = " ".join(_block_text(b) for b in extraction_blocks)
    applicability = document_applies_to_pathway(
        pathway_id=pathway_id,
        source_url=source_url,
        title=title,
        official_url=official_url,
        body_text=full_text,
        require_body_markers=True,
    )
    if not applicability.applies:
        return None

    matched, cue, passage = extract_requirement_passage(extraction_blocks, requirement)
    if not matched or len(passage) < MIN_PASSAGE_CHARS:
        return None

    support = passage_supports_requirement(
        pathway_id=pathway_id,
        requirement=requirement,
        passage=passage,
        source_url=source_url,
        title=title,
    )
    if not support.ok:
        # Exemptions for financial may yield NOT_APPLICABLE rather than PARTIAL.
        if support.kind == "exemption" and support.suggested_classification == "NOT_APPLICABLE":
            return Assessment(
                pathway_id=pathway_id,
                requirement=support.suggested_requirement or requirement,
                prev_classification="UNEVALUATED",
                new_classification="NOT_APPLICABLE",
                document_id=document_id,
                version_id=version_id,
                source_url=source_url,
                passage=passage[:500],
                rationale=(
                    f"Pathway-applicable ({applicability.basis}); exemption: {support.reason}. "
                    "Not human_verified."
                ),
                method="requirement_support_exemption",
                confidence="medium",
                applicability_basis=applicability.basis,
            )
        return None
    if support.suggested_classification == "NOT_APPLICABLE":
        return None  # do not auto-upgrade claimed requirement via exemption side-effect

    return Assessment(
        pathway_id=pathway_id,
        requirement=requirement,
        prev_classification="UNEVALUATED",
        new_classification="PARTIAL",
        document_id=document_id,
        version_id=version_id,
        source_url=source_url,
        passage=passage[:500],
        rationale=(
            f"Pathway-applicable ({applicability.basis}); requirement support ({support.reason}); "
            f"cue {cue}. Machine assessment only — never auto COMPLETE/human_verified."
        ),
        method="pathway_requirement_aware_machine",
        confidence="medium" if len(passage) >= 100 else "low",
        applicability_basis=applicability.basis,
    )


def validate_existing_association(
    *,
    pathway_id: str,
    requirement: str,
    source_url: str,
    passage: str,
    title: str = "",
    official_url: str = "",
    classification: str = "",
) -> tuple[bool, str]:
    """Return (ok, reason). False means the association should be rejected/downgraded."""
    if requirement == "official_provenance":
        return True, "provenance_not_content_gated"
    if classification in ("", "UNEVALUATED", "MISSING", "NOT_APPLICABLE", "BLOCKED"):
        return True, "no_positive_claim"
    applicability = document_applies_to_pathway(
        pathway_id=pathway_id,
        source_url=source_url,
        title=title,
        official_url=official_url,
        body_text=passage or "",
    )
    if not applicability.applies:
        return False, applicability.reason
    if classification in ("PARTIAL", "COMPLETE") and (not passage or len(passage) < MIN_PASSAGE_CHARS):
        return False, f"insufficient_passage:{len(passage or '')}"
    support = validate_evidence_claim(
        pathway_id=pathway_id,
        requirement=requirement,
        passage=passage or "",
        source_url=source_url,
        title=title,
        classification=classification,
    )
    if not support.ok:
        return False, support.reason
    return True, f"{applicability.basis};{support.reason}"


def reassess_suspicious_rows(
    evidence_rows: list[dict[str, Any]],
    *,
    prior_machine_changes: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Downgrade invalid PARTIAL/COMPLETE associations; preserve audit via changes.

    Prioritizes Phase 6I machine assessments and other broad keyword matches.
    Returns (updated_rows, change_records).
    """
    rows = [dict(r) for r in evidence_rows]
    by_key = {(r["pathway_id"], r["requirement"]): r for r in rows}
    changes: list[dict[str, Any]] = []

    suspect_keys: set[tuple[str, str]] = set()
    if prior_machine_changes:
        for c in prior_machine_changes:
            suspect_keys.add((c["pathway_id"], c["requirement"]))

    for r in rows:
        if r.get("entity_type") != "true_pathway":
            continue
        cls = get_classification(r)
        if cls not in ("PARTIAL", "COMPLETE"):
            continue
        key = (r["pathway_id"], r["requirement"])
        method = (r.get("method") or r.get("evidence_basis") or r.get("evidence_repair") or "")
        is_machine = (
            key in suspect_keys
            or "machine_assessed" in str(method)
            or "auto_match" in str(method)
            or "phase_6i" in str(method)
            or "pathway_aware" in str(method)
            or "pathway_requirement_aware" in str(method)
            or "phase_6j" in str(method)
        )
        # Always validate machine / auto-match; also validate COMPLETE/PARTIAL with URLs
        # that fail pathway applicability (suspicious cross-links).
        ok, reason = validate_existing_association(
            pathway_id=r["pathway_id"],
            requirement=r["requirement"],
            source_url=r.get("source_url") or r.get("url") or "",
            passage=r.get("passage") or "",
            title=r.get("title") or "",
            official_url=r.get("official_url") or "",
            classification=cls,
        )
        # Machine / auto-match cells: reject any failed applicability.
        # Curated cells: only reject clear cross-pathway exclusions or generic hubs.
        if ok:
            continue
        if not is_machine:
            if "hard_exclusion" not in reason and "generic_hub" not in reason:
                continue

        prev = cls
        # Downgrade invalid positive evidence to UNEVALUATED (uncertain) — do not invent MISSING.
        r["classification"] = "UNEVALUATED"
        if "classification_6h" in r:
            r["classification_6h"] = "UNEVALUATED"
        r["evidence_repair"] = "phase_6j_pathway_aware_reject"
        r["method"] = "pathway_aware_reassess"
        r["rationale"] = reason
        # Preserve prior refs in change log; clear from active cell so they cannot drive readiness.
        change = {
            "pathway_id": r["pathway_id"],
            "requirement": r["requirement"],
            "prev_classification": prev,
            "new_classification": "UNEVALUATED",
            "document_id": r.get("document_id") or "",
            "version_id": r.get("version_id") or "",
            "source_url": r.get("source_url") or "",
            "passage": (r.get("passage") or "")[:200],
            "rationale": reason,
            "method": "pathway_aware_reassess",
            "action": "reject_invalid_association",
        }
        r["document_id"] = ""
        r["version_id"] = ""
        r["passage"] = ""
        r["source_url"] = r.get("official_url") or ""
        changes.append(change)
        by_key[key] = r

    return rows, changes


def batch_assess(
    session: Session,
    evidence_rows: list[dict[str, Any]],
    data_dir: Path,
    max_assessments: int = 100,
    priority_pathways: list[str] | None = None,
    inventory_official_urls: dict[str, str] | None = None,
) -> tuple[list[Assessment], list[dict[str, Any]]]:
    """Batch assess UNEVALUATED cells using pathway-aware matching only."""
    pathways = priority_pathways or PRIORITY_PATHWAYS
    official = inventory_official_urls or {}

    unevaluated = [
        r for r in evidence_rows
        if get_classification(r) == "UNEVALUATED"
        and r["pathway_id"] in pathways
        and r.get("entity_type") == "true_pathway"
        and r["requirement"] != "official_provenance"
    ]

    doc_rows = session.execute(
        text("""
            SELECT d.id, d.canonical_url, d.url_normalized, d.title, d.primary_source_id,
                   s.jurisdiction_id, j.country_code
            FROM documents d
            JOIN sources s ON s.id = d.primary_source_id
            JOIN jurisdictions j ON j.id = s.jurisdiction_id
        """)
    ).mappings().all()

    docs_by_country: dict[str, list[dict[str, Any]]] = {}
    for r in doc_rows:
        cc = r["country_code"]
        if cc:
            docs_by_country.setdefault(cc, []).append(dict(r))

    version_rows = session.execute(
        text("SELECT dv.id, dv.document_id FROM document_versions dv WHERE dv.is_current = true")
    ).mappings().all()
    current_versions = {int(r["document_id"]): int(r["id"]) for r in version_rows}

    extraction_rows = session.execute(
        text("""
            SELECT de.document_version_id, de.storage_path, de.status
            FROM document_extractions de
            WHERE de.status = 'success'
        """)
    ).mappings().all()
    extractions_by_version = {int(r["document_version_id"]): dict(r) for r in extraction_rows}

    assessments: list[Assessment] = []
    changes: list[dict[str, Any]] = []

    for row in unevaluated:
        if len(assessments) >= max_assessments:
            break

        pid = row["pathway_id"]
        req = row["requirement"]
        country = row.get("country", "")
        # Prefer docs whose URL already matches pathway tokens.
        country_docs = docs_by_country.get(country, [])
        tokens = PATHWAY_URL_TOKENS.get(pid, ())

        def sort_key(doc: dict[str, Any]) -> tuple[int, int]:
            key = (doc.get("url_normalized") or doc.get("canonical_url") or "").lower()
            hit = 0 if any(t in key for t in tokens) else 1
            return (hit, int(doc["id"]))

        for doc in sorted(country_docs, key=sort_key):
            doc_id = int(doc["id"])
            ver_id = current_versions.get(doc_id)
            if not ver_id:
                continue
            ext = extractions_by_version.get(ver_id)
            if not ext or not ext.get("storage_path"):
                continue
            ext_path = data_dir / ext["storage_path"]
            if not ext_path.is_file():
                # storage_path may already be absolute under data/
                alt = Path(ext["storage_path"])
                ext_path = alt if alt.is_file() else data_dir / str(ext["storage_path"]).lstrip("/")
            if not ext_path.is_file():
                continue
            try:
                extraction = json.loads(ext_path.read_text(encoding="utf-8"))
                blocks = extraction.get("blocks") or extraction.get("content", {}).get("blocks", [])
            except (json.JSONDecodeError, OSError, KeyError):
                continue

            assessment = assess_document_for_pathway(
                blocks,
                pid,
                req,
                doc_id,
                ver_id,
                doc["canonical_url"],
                title=doc.get("title") or "",
                official_url=official.get(pid) or row.get("official_url") or "",
            )
            if assessment:
                assessment.prev_classification = get_classification(row)
                assessments.append(assessment)
                changes.append({
                    "pathway_id": pid,
                    "requirement": req,
                    "prev_classification": get_classification(row),
                    "new_classification": assessment.new_classification,
                    "document_id": doc_id,
                    "version_id": ver_id,
                    "source_url": doc["canonical_url"],
                    "passage": assessment.passage[:200],
                    "rationale": assessment.rationale,
                    "method": assessment.method,
                    "action": "pathway_aware_upgrade",
                    "applicability_basis": assessment.applicability_basis,
                })
                break

    return assessments, changes


def apply_assessments_to_rows(
    evidence_rows: list[dict[str, Any]],
    assessments: list[Assessment],
) -> list[dict[str, Any]]:
    """Apply accepted assessments onto evidence rows.

    Allows PARTIAL and explicit NOT_APPLICABLE (exemptions). Never COMPLETE.
    Only fills UNEVALUATED/MISSING cells (does not overwrite COMPLETE).
    """
    rows = [dict(r) for r in evidence_rows]
    index = {(r["pathway_id"], r["requirement"]): r for r in rows}
    for a in assessments:
        row = index.get((a.pathway_id, a.requirement))
        if not row:
            continue
        if get_classification(row) not in ("UNEVALUATED", "MISSING"):
            continue
        if a.new_classification == "COMPLETE":
            continue  # hard guard
        if a.new_classification not in ("PARTIAL", "NOT_APPLICABLE"):
            continue
        row["classification"] = a.new_classification
        if "classification_6h" in row:
            row["classification_6h"] = a.new_classification
        row["document_id"] = a.document_id or ""
        row["version_id"] = a.version_id or ""
        row["source_url"] = a.source_url
        row["passage"] = a.passage
        row["method"] = a.method
        row["evidence_repair"] = (
            "phase_6k_exemption"
            if a.new_classification == "NOT_APPLICABLE"
            else "phase_6k_pathway_requirement_partial"
        )
        row["rationale"] = a.rationale
        if a.new_classification == "NOT_APPLICABLE":
            row["not_applicable_rationale"] = a.rationale
    return rows


def write_assessments_csv(assessments: list[Assessment], path: Path) -> None:
    fieldnames = [
        "pathway_id", "requirement", "prev_classification", "new_classification",
        "document_id", "version_id", "source_url", "passage", "rationale",
        "method", "confidence", "applicability_basis", "assessed_at",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for a in assessments:
            writer.writerow({
                "pathway_id": a.pathway_id,
                "requirement": a.requirement,
                "prev_classification": a.prev_classification,
                "new_classification": a.new_classification,
                "document_id": a.document_id or "",
                "version_id": a.version_id or "",
                "source_url": a.source_url,
                "passage": a.passage,
                "rationale": a.rationale,
                "method": a.method,
                "confidence": a.confidence,
                "applicability_basis": a.applicability_basis,
                "assessed_at": a.assessed_at,
            })


def write_changes_csv(changes: list[dict[str, Any]], path: Path) -> None:
    if not changes:
        path.write_text(
            "pathway_id,requirement,prev_classification,new_classification,"
            "document_id,version_id,source_url,passage,rationale,method,action\n",
            encoding="utf-8",
        )
        return
    fieldnames = [
        "pathway_id", "requirement", "prev_classification", "new_classification",
        "document_id", "version_id", "source_url", "passage", "rationale",
        "method", "action", "applicability_basis",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(changes)
