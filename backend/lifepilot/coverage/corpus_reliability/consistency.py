"""Consistency audit for evidence/provenance matrix.

Detects issues using existing 6H evidence matrix + DB:
- COMPLETE without supporting version
- Provenance MISSING when relevant extracted docs exist
- URL-token-only associations
- Hub-as-pathway errors
- Duplicates/query variants (gov.uk student print, USCIS query string)
- Wrong pathway links
- EU evidence on national pathways
- Orphans/unextracted documents
"""

from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifepilot.coverage.pathway_inventory.inventory import build_inventory_rows

# Known print/query variant patterns
PRINT_VARIANT_PATTERNS = [
    (r"gov\.uk.*[?&]print=", "gov.uk print variant"),
    (r"uscis\.gov.*[?&]", "USCIS query string variant"),
    (r"[?&]utm_", "UTM tracking parameter"),
    (r"[?&]fbclid=", "Facebook tracking parameter"),
]

EU_FRAMEWORK_KEYWORDS = [
    "eu_blue_card",
    "eu_framework",
    "european_union",
    "directive_2009",
    "directive_2021",
]


def is_query_variant(url: str) -> tuple[bool, str]:
    """Check if URL is a query string variant (print, tracking, etc.)."""
    for pattern, reason in PRINT_VARIANT_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return True, reason
    return False, ""


def is_eu_evidence(url: str, pathway_id: str) -> bool:
    """Check if URL/pathway is EU framework evidence."""
    lower_url = url.lower()
    lower_pid = pathway_id.lower()
    return any(kw in lower_url or kw in lower_pid for kw in EU_FRAMEWORK_KEYWORDS)


def load_evidence_matrix(csv_path: Path) -> list[dict[str, Any]]:
    """Load evidence matrix from CSV."""
    with csv_path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def audit_evidence_consistency(
    session: Session,
    evidence_rows: list[dict[str, Any]],
    inventory_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Audit evidence matrix for consistency issues.
    
    Returns list of issues with:
    - issue_type: Category of the issue
    - pathway_id: Affected pathway
    - requirement: Affected requirement
    - severity: high/medium/low
    - description: Human-readable description
    - auto_repairable: Whether this can be auto-fixed
    - repair_action: What repair would do
    """
    inventory = inventory_rows if inventory_rows is not None else build_inventory_rows()
    inventory_by_id = {r["pathway_id"]: r for r in inventory}
    
    issues: list[dict[str, Any]] = []
    
    # Get document/version/extraction data from DB
    docs_by_id = {}
    versions_by_doc = {}
    extractions_by_version = {}
    
    doc_rows = session.execute(
        text("""
            SELECT d.id, d.canonical_url, d.url_normalized, d.title,
                   d.primary_source_id, d.discovery_status
            FROM documents d
        """)
    ).mappings().all()
    for r in doc_rows:
        docs_by_id[int(r["id"])] = dict(r)
    
    version_rows = session.execute(
        text("""
            SELECT dv.id, dv.document_id, dv.storage_path, dv.content_sha256, dv.is_current
            FROM document_versions dv
        """)
    ).mappings().all()
    for r in version_rows:
        versions_by_doc.setdefault(int(r["document_id"]), []).append(dict(r))
    
    extraction_rows = session.execute(
        text("""
            SELECT de.id, de.document_version_id, de.status, de.storage_path, de.content_sha256
            FROM document_extractions de
        """)
    ).mappings().all()
    for r in extraction_rows:
        extractions_by_version.setdefault(int(r["document_version_id"]), []).append(dict(r))
    
    for row in evidence_rows:
        pid = row["pathway_id"]
        req = row["requirement"]
        # Support both "classification" and "classification_6h" column names
        cls = row.get("classification") or row.get("classification_6h", "UNEVALUATED")
        doc_id = row.get("document_id")
        ver_id = row.get("version_id")
        url = row.get("source_url", "") or row.get("url", "")
        entity_type = row.get("entity_type", "")
        inv = inventory_by_id.get(pid, {})
        
        # 1. COMPLETE without supporting version
        if cls == "COMPLETE" and doc_id not in ("", None):
            try:
                doc_id_int = int(doc_id)
                versions = versions_by_doc.get(doc_id_int, [])
                if not versions:
                    issues.append({
                        "issue_type": "complete_without_version",
                        "pathway_id": pid,
                        "requirement": req,
                        "severity": "high",
                        "description": f"COMPLETE cell references document {doc_id} which has no versions",
                        "auto_repairable": False,
                        "repair_action": "Manual review required - document may be orphaned",
                        "document_id": doc_id,
                        "version_id": ver_id,
                        "url": url,
                    })
                elif ver_id not in ("", None):
                    ver_id_int = int(ver_id)
                    if not any(v["id"] == ver_id_int for v in versions):
                        issues.append({
                            "issue_type": "complete_invalid_version",
                            "pathway_id": pid,
                            "requirement": req,
                            "severity": "high",
                            "description": f"COMPLETE cell references version {ver_id} which doesn't exist for document {doc_id}",
                            "auto_repairable": True,
                            "repair_action": "Update to current version or clear invalid reference",
                            "document_id": doc_id,
                            "version_id": ver_id,
                            "url": url,
                        })
            except (ValueError, TypeError):
                pass
        
        # 2. Provenance MISSING when relevant extracted docs exist
        if req == "official_provenance" and cls in ("MISSING", "UNEVALUATED"):
            if inv.get("ingested_documents", 0) > 0:
                issues.append({
                    "issue_type": "provenance_missing_with_docs",
                    "pathway_id": pid,
                    "requirement": req,
                    "severity": "medium",
                    "description": f"Provenance {cls} but pathway has {inv.get('ingested_documents')} ingested documents",
                    "auto_repairable": True,
                    "repair_action": "Upgrade to PARTIAL with evidence from ingested documents",
                    "document_id": doc_id,
                    "version_id": ver_id,
                    "url": url,
                })
        
        # 3. URL query string variants
        if url:
            is_variant, variant_reason = is_query_variant(url)
            if is_variant:
                issues.append({
                    "issue_type": "query_string_variant",
                    "pathway_id": pid,
                    "requirement": req,
                    "severity": "low",
                    "description": f"URL is a {variant_reason}: {url[:100]}",
                    "auto_repairable": True,
                    "repair_action": "Normalize URL by removing query parameters",
                    "document_id": doc_id,
                    "version_id": ver_id,
                    "url": url,
                })
        
        # 4. Hub-as-pathway (information_hub marked as COMPLETE for content requirements)
        if entity_type == "information_hub" and req != "official_provenance":
            if cls == "COMPLETE":
                issues.append({
                    "issue_type": "hub_as_pathway",
                    "pathway_id": pid,
                    "requirement": req,
                    "severity": "high",
                    "description": f"Information hub {pid} has COMPLETE for {req} - should be NOT_APPLICABLE",
                    "auto_repairable": True,
                    "repair_action": "Set classification to NOT_APPLICABLE for hub content requirements",
                    "document_id": doc_id,
                    "version_id": ver_id,
                    "url": url,
                })
        
        # 5. EU evidence on national pathways
        if entity_type == "true_pathway" and not pid.startswith("eu_"):
            if url and is_eu_evidence(url, "") and not is_eu_evidence("", pid):
                # EU document linked to non-EU pathway
                issues.append({
                    "issue_type": "eu_evidence_on_national",
                    "pathway_id": pid,
                    "requirement": req,
                    "severity": "medium",
                    "description": f"EU framework evidence linked to national pathway {pid}",
                    "auto_repairable": False,
                    "repair_action": "Manual review - verify if EU evidence is applicable to national implementation",
                    "document_id": doc_id,
                    "version_id": ver_id,
                    "url": url,
                })
    
    # 6. Documents with extractions but unused in the evidence matrix are
    # unused/unlinked candidates — not integrity failures. Reserve
    # orphaned_document for broken references (COMPLETE pointing at missing docs).
    referenced_doc_ids = set()
    for row in evidence_rows:
        if row.get("document_id") not in ("", None):
            try:
                referenced_doc_ids.add(int(row["document_id"]))
            except (ValueError, TypeError):
                pass

    for doc_id, doc in docs_by_id.items():
        if doc_id not in referenced_doc_ids:
            versions = versions_by_doc.get(doc_id, [])
            has_extraction = any(
                any(e["status"] == "success" for e in extractions_by_version.get(v["id"], []))
                for v in versions
            )
            has_any_version = bool(versions)
            if has_extraction:
                issues.append({
                    "issue_type": "unused_unlinked_document",
                    "pathway_id": "",
                    "requirement": "",
                    "severity": "info",
                    "description": (
                        f"Document {doc_id} ({doc['title'] or (doc['canonical_url'] or '')[:50]}) "
                        "has a successful extraction but is not linked in the evidence matrix"
                    ),
                    "auto_repairable": False,
                    "repair_action": "Optional: review for pathway-specific linkage; not an integrity error",
                    "document_id": doc_id,
                    "version_id": "",
                    "url": doc["canonical_url"],
                })
            elif not has_any_version:
                issues.append({
                    "issue_type": "orphaned_document",
                    "pathway_id": "",
                    "requirement": "",
                    "severity": "medium",
                    "description": (
                        f"Document {doc_id} ({doc['title'] or (doc['canonical_url'] or '')[:50]}) "
                        "has no versions — broken corpus record"
                    ),
                    "auto_repairable": False,
                    "repair_action": "Investigate missing versions / storage",
                    "document_id": doc_id,
                    "version_id": "",
                    "url": doc["canonical_url"],
                })
    
    # 7. Check for duplicate documents (same normalized URL)
    url_to_docs: dict[str, list[int]] = {}
    for doc_id, doc in docs_by_id.items():
        norm_url = doc["url_normalized"]
        url_to_docs.setdefault(norm_url, []).append(doc_id)
    
    for norm_url, doc_ids in url_to_docs.items():
        if len(doc_ids) > 1:
            issues.append({
                "issue_type": "duplicate_documents",
                "pathway_id": "",
                "requirement": "",
                "severity": "medium",
                "description": f"Multiple documents for URL {norm_url[:80]}: IDs {doc_ids}",
                "auto_repairable": False,
                "repair_action": "Consolidate duplicate document records",
                "document_id": str(doc_ids[0]),
                "version_id": "",
                "url": norm_url,
            })
    
    return issues


def generate_repair_proposals(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate repair proposals for auto-repairable issues.
    
    Returns list of repair proposals with:
    - issue_type: Original issue type
    - pathway_id: Affected pathway
    - requirement: Affected requirement
    - current_value: Current state
    - proposed_value: Proposed new state
    - rationale: Why this repair is safe
    - reversible: Whether this can be undone
    """
    repairs: list[dict[str, Any]] = []
    
    for issue in issues:
        if not issue.get("auto_repairable"):
            continue
        
        if issue["issue_type"] == "complete_invalid_version":
            repairs.append({
                "issue_type": issue["issue_type"],
                "pathway_id": issue["pathway_id"],
                "requirement": issue["requirement"],
                "current_value": f"version_id={issue['version_id']}",
                "proposed_value": "version_id=(resolve to current version)",
                "rationale": "Referenced version doesn't exist; resolve to document's current version",
                "reversible": True,
                "document_id": issue["document_id"],
            })
        
        elif issue["issue_type"] == "provenance_missing_with_docs":
            repairs.append({
                "issue_type": issue["issue_type"],
                "pathway_id": issue["pathway_id"],
                "requirement": issue["requirement"],
                "current_value": f"classification=MISSING/UNEVALUATED",
                "proposed_value": "classification=PARTIAL (ingested docs exist)",
                "rationale": "Ingested documents exist for this pathway; provenance can be upgraded to PARTIAL",
                "reversible": True,
                "document_id": issue["document_id"],
            })
        
        elif issue["issue_type"] == "query_string_variant":
            parsed = urlparse(issue["url"])
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            repairs.append({
                "issue_type": issue["issue_type"],
                "pathway_id": issue["pathway_id"],
                "requirement": issue["requirement"],
                "current_value": f"url={issue['url'][:80]}",
                "proposed_value": f"url={clean_url[:80]}",
                "rationale": "Normalize URL by removing tracking/print query parameters",
                "reversible": True,
                "document_id": issue["document_id"],
            })
        
        elif issue["issue_type"] == "hub_as_pathway":
            repairs.append({
                "issue_type": issue["issue_type"],
                "pathway_id": issue["pathway_id"],
                "requirement": issue["requirement"],
                "current_value": "classification=COMPLETE",
                "proposed_value": "classification=NOT_APPLICABLE",
                "rationale": "Information hubs should have NOT_APPLICABLE for content requirements",
                "reversible": True,
                "document_id": issue["document_id"],
            })
    
    return repairs


def write_audit_csv(issues: list[dict[str, Any]], path: Path) -> None:
    """Write consistency audit to CSV."""
    if not issues:
        path.write_text("issue_type,pathway_id,requirement,severity,description,auto_repairable,repair_action,document_id,version_id,url\n", encoding="utf-8")
        return
    
    fieldnames = [
        "issue_type", "pathway_id", "requirement", "severity",
        "description", "auto_repairable", "repair_action",
        "document_id", "version_id", "url"
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(issues)


def write_repairs_csv(repairs: list[dict[str, Any]], path: Path) -> None:
    """Write repair proposals to CSV."""
    if not repairs:
        path.write_text("issue_type,pathway_id,requirement,current_value,proposed_value,rationale,reversible,document_id\n", encoding="utf-8")
        return
    
    fieldnames = [
        "issue_type", "pathway_id", "requirement",
        "current_value", "proposed_value", "rationale", "reversible", "document_id"
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(repairs)
