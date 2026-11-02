"""Evidence assessment for existing corpus documents.

Reduces UNEVALUATED cells by assessing already-ingested docs against
priority pathways. Never forces assessments - only upgrades cells where
evidence genuinely supports it.

Rules:
- COMPLETE requires: passage + document_id + version_id
- Never sets human_verified=true (machine_assessed only)
- No invented requirements
- Configurable batch size
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

# Priority pathways for assessment (from user spec)
PRIORITY_PATHWAYS = [
    # GB student
    "gb_student", "gb_skilled_worker", "gb_graduate",
    # DE blue card
    "de_eu_blue_card",
    # NL routes
    "nl_highly_skilled_migrant", "nl_naturalisation", "nl_permanent_residence",
    # US H/L/O/E
    "us_h1b", "us_l1", "us_o1", "us_e2", "us_f1",
    # AU 485
    "au_temporary_graduate_485",
    # CA FSW/CEC/FST
    "ca_federal_skilled_worker", "ca_canadian_experience_class", "ca_federal_skilled_trades",
    # NZ
    "nz_skilled_migrant", "nz_essential_skills", "nz_citizenship_by_grant",
    # SG PR
    "sg_permanent_residence", "sg_employment_pass",
    # IE
    "ie_critical_skills_employment_permit", "ie_general_employment_permit",
]

CRITICAL_REQUIREMENTS = [
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
]

# Keyword patterns for requirement matching
REQUIREMENT_PATTERNS = {
    "eligibility": [
        r"eligib[a-z]*",
        r"requirement[s]?\s+(to|for)",
        r"who\s+can\s+apply",
        r"must\s+(be|have)",
        r"qualif[a-z]*",
        r"criteria",
    ],
    "required_documents": [
        r"document[s]?\s+(required|needed|checklist)",
        r"supporting\s+document",
        r"you\s+(will\s+)?need\s+to\s+(provide|submit)",
        r"checklist",
        r"evidence\s+(you\s+)?need",
    ],
    "application_process": [
        r"how\s+to\s+apply",
        r"application\s+(process|procedure)",
        r"step[s]?\s+to\s+apply",
        r"submit\s+(your\s+)?application",
        r"apply\s+(online|by\s+post)",
    ],
    "financial_or_sponsorship": [
        r"financial\s+requirement",
        r"proof\s+of\s+funds",
        r"maintenance\s+funds",
        r"sponsor(ship)?",
        r"salary\s+(threshold|requirement)",
        r"income\s+requirement",
    ],
}


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
    method: str = "machine_assessed"
    confidence: str = "medium"
    assessed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def match_requirement(text: str, requirement: str) -> tuple[bool, str, str]:
    """Check if text matches a requirement's patterns.
    
    Returns (matched, matched_pattern, passage_excerpt).
    """
    patterns = REQUIREMENT_PATTERNS.get(requirement, [])
    text_lower = text.lower()
    
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            # Extract context around match
            start = max(0, match.start() - 100)
            end = min(len(text), match.end() + 300)
            passage = text[start:end].strip()
            return True, pattern, passage
    
    return False, "", ""


def assess_document_for_pathway(
    extraction_blocks: list[dict[str, Any]],
    pathway_id: str,
    requirement: str,
    document_id: int,
    version_id: int,
    source_url: str,
) -> Assessment | None:
    """Assess a single document for a pathway/requirement.
    
    Returns Assessment if evidence found, None otherwise.
    """
    if requirement == "official_provenance":
        # Provenance is assessed differently (via source linkage)
        return None
    
    # Combine all text blocks
    full_text = ""
    for block in extraction_blocks:
        if block.get("type") in ("heading", "paragraph", "list_item", "table_cell"):
            full_text += " " + (block.get("text") or "")
    
    matched, pattern, passage = match_requirement(full_text, requirement)
    
    if matched and len(passage) >= 50:
        return Assessment(
            pathway_id=pathway_id,
            requirement=requirement,
            prev_classification="UNEVALUATED",
            new_classification="PARTIAL",  # Never COMPLETE from machine assessment
            document_id=document_id,
            version_id=version_id,
            source_url=source_url,
            passage=passage[:500],
            rationale=f"Pattern matched: {pattern}. Machine assessment - requires human verification for COMPLETE.",
            method="machine_assessed",
            confidence="medium" if len(passage) >= 100 else "low",
        )
    
    return None


def load_extractions_for_pathway(
    session: Session,
    pathway_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Load extractions for documents linked to given pathways.
    
    Returns dict[pathway_id, list of extraction records].
    """
    # This is a simplified version - in reality we'd need to join
    # through the evidence matrix or source linkages
    result = session.execute(
        text("""
            SELECT d.id AS document_id, d.canonical_url, d.title,
                   dv.id AS version_id, dv.is_current,
                   de.storage_path AS extraction_path, de.status
            FROM documents d
            JOIN document_versions dv ON dv.document_id = d.id
            JOIN document_extractions de ON de.document_version_id = dv.id
            WHERE de.status = 'success'
              AND dv.is_current = true
            ORDER BY d.id
        """)
    ).mappings().all()
    
    return {
        "all": [dict(r) for r in result]
    }


def batch_assess(
    session: Session,
    evidence_rows: list[dict[str, Any]],
    data_dir: Path,
    max_assessments: int = 100,
    priority_pathways: list[str] | None = None,
) -> tuple[list[Assessment], list[dict[str, Any]]]:
    """Batch assess UNEVALUATED cells against existing documents.
    
    Args:
        session: Database session.
        evidence_rows: Current evidence matrix rows.
        data_dir: Path to data/ directory for reading extractions.
        max_assessments: Maximum number of assessments to generate.
        priority_pathways: Pathways to prioritize (default: PRIORITY_PATHWAYS).
    
    Returns:
        (assessments, changes) - list of new assessments and evidence changes.
    """
    pathways = priority_pathways or PRIORITY_PATHWAYS
    
    # Find UNEVALUATED cells for priority pathways
    # Support both "classification" and "classification_6h" column names
    def get_cls(r: dict) -> str:
        return r.get("classification") or r.get("classification_6h", "UNEVALUATED")
    
    unevaluated = [
        r for r in evidence_rows
        if get_cls(r) == "UNEVALUATED"
        and r["pathway_id"] in pathways
        and r["entity_type"] == "true_pathway"
        and r["requirement"] != "official_provenance"
    ]
    
    # Get document/extraction data
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
        text("""
            SELECT dv.id, dv.document_id, dv.is_current
            FROM document_versions dv
            WHERE dv.is_current = true
        """)
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
        
        # Get documents for this country
        country_docs = docs_by_country.get(country, [])
        
        for doc in country_docs:
            doc_id = int(doc["id"])
            ver_id = current_versions.get(doc_id)
            if not ver_id:
                continue
            
            ext = extractions_by_version.get(ver_id)
            if not ext or not ext.get("storage_path"):
                continue
            
            # Load extraction JSON
            ext_path = data_dir / ext["storage_path"]
            if not ext_path.is_file():
                continue
            
            try:
                extraction = json.loads(ext_path.read_text(encoding="utf-8"))
                blocks = extraction.get("blocks") or extraction.get("content", {}).get("blocks", [])
            except (json.JSONDecodeError, KeyError):
                continue
            
            assessment = assess_document_for_pathway(
                blocks, pid, req, doc_id, ver_id, doc["canonical_url"]
            )
            
            if assessment:
                assessment.prev_classification = get_cls(row)
                assessments.append(assessment)
                changes.append({
                    "pathway_id": pid,
                    "requirement": req,
                    "prev_classification": get_cls(row),
                    "new_classification": assessment.new_classification,
                    "document_id": doc_id,
                    "version_id": ver_id,
                    "source_url": doc["canonical_url"],
                    "passage": assessment.passage[:200],
                    "rationale": assessment.rationale,
                    "method": assessment.method,
                })
                break  # One assessment per cell
    
    return assessments, changes


def write_assessments_csv(assessments: list[Assessment], path: Path) -> None:
    """Write assessments to CSV."""
    fieldnames = [
        "pathway_id", "requirement", "prev_classification", "new_classification",
        "document_id", "version_id", "source_url", "passage", "rationale",
        "method", "confidence", "assessed_at"
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
                "assessed_at": a.assessed_at,
            })


def write_changes_csv(changes: list[dict[str, Any]], path: Path) -> None:
    """Write evidence changes to CSV."""
    if not changes:
        path.write_text("pathway_id,requirement,prev_classification,new_classification,document_id,version_id,source_url,passage,rationale,method\n", encoding="utf-8")
        return
    
    fieldnames = [
        "pathway_id", "requirement", "prev_classification", "new_classification",
        "document_id", "version_id", "source_url", "passage", "rationale", "method"
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(changes)
