"""Readiness audit for provisionally ready pathways.

Independently audits pathways marked as provisionally ready to verify
they genuinely meet readiness criteria. Especially checks that IE
readiness isn't checklist-docs-only.

Decisions: retain / downgrade / requires_manual_review
Never sets human_verified=true.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Pathways to audit (from user spec)
AUDIT_PATHWAYS = [
    "gb_skilled_worker",
    "nl_highly_skilled_migrant",
    "nl_naturalisation",
    "sg_employment_pass",
    "ie_critical_skills_employment_permit",
    "ie_general_employment_permit",
]

CRITICAL_REQUIREMENTS = [
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
]

# IE checklist-only evidence patterns
IE_CHECKLIST_PATTERNS = [
    "checklist",
    "documents required",
    "supporting documents",
]


@dataclass
class ReadinessReview:
    """A single pathway readiness review."""
    pathway_id: str
    country: str
    was_provisionally_ready: bool
    decision: str  # retain / downgrade / requires_manual_review
    rationale: str
    evidence_summary: dict[str, str] = field(default_factory=dict)
    concerns: list[str] = field(default_factory=list)
    reviewed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    human_verified: bool = False  # Always False


def check_evidence_quality(
    evidence_rows: list[dict[str, Any]],
    pathway_id: str,
) -> dict[str, Any]:
    """Check quality of evidence for a pathway.
    
    Returns summary of evidence quality per requirement.
    """
    pathway_rows = [
        r for r in evidence_rows
        if r["pathway_id"] == pathway_id and r["entity_type"] == "true_pathway"
    ]
    
    summary = {}
    concerns = []
    
    for req in CRITICAL_REQUIREMENTS:
        req_rows = [r for r in pathway_rows if r["requirement"] == req]
        if not req_rows:
            summary[req] = "MISSING_ROW"
            concerns.append(f"{req}: No evidence row found")
            continue
        
        row = req_rows[0]
        # Support both "classification" and "classification_6h" column names
        cls = row.get("classification") or row.get("classification_6h", "UNEVALUATED")
        summary[req] = cls
        
        if cls == "COMPLETE":
            # Verify COMPLETE has substantive evidence
            passage = row.get("passage", "")
            doc_id = row.get("document_id")
            
            if not doc_id:
                concerns.append(f"{req}: COMPLETE without document_id")
            
            if not passage or len(passage) < 50:
                concerns.append(f"{req}: COMPLETE with insufficient passage ({len(passage or '')} chars)")
            
            # Check for checklist-only evidence
            if req in ("eligibility", "application_process"):
                passage_lower = (passage or "").lower()
                if any(p in passage_lower for p in IE_CHECKLIST_PATTERNS):
                    if not any(
                        kw in passage_lower
                        for kw in ("criteria", "requirement", "eligible", "how to apply", "process")
                    ):
                        concerns.append(
                            f"{req}: Evidence may be checklist-docs-only "
                            f"(found checklist keywords but no substantive content)"
                        )
        
        elif cls in ("MISSING", "UNEVALUATED", "PARTIAL"):
            # Provisional readiness requires all applicable critical fields COMPLETE
            # (or NOT_APPLICABLE). PARTIAL blocks retain.
            if cls != "NOT_APPLICABLE":
                concerns.append(f"{req}: Classification is {cls}")
    
    applicable_ok = all(
        s in {"COMPLETE", "NOT_APPLICABLE"} for s in summary.values()
    )
    return {
        "summary": summary,
        "concerns": concerns,
        "all_complete": applicable_ok,
        "has_concerns": len(concerns) > 0,
    }


def audit_ie_pathway(
    evidence_rows: list[dict[str, Any]],
    pathway_id: str,
) -> dict[str, Any]:
    """Special audit for IE pathways - check for checklist-docs-only evidence.
    
    IE readiness must not be based solely on document checklists.
    """
    pathway_rows = [
        r for r in evidence_rows
        if r["pathway_id"] == pathway_id and r["entity_type"] == "true_pathway"
    ]
    
    ie_concerns = []
    
    for row in pathway_rows:
        req = row.get("requirement")
        # Support both "classification" and "classification_6h" column names
        cls = row.get("classification") or row.get("classification_6h", "UNEVALUATED")
        passage = row.get("passage", "") or row.get("reason", "")
        evidence_basis = row.get("evidence_basis", "")
        
        if cls == "COMPLETE" and req in ("eligibility", "required_documents", "application_process"):
            # Check if evidence is substantive or just a checklist reference
            passage_lower = (passage or "").lower()
            
            is_checklist_only = (
                "checklist" in passage_lower or
                "documents required" in passage_lower or
                evidence_basis in ("official_checklist_pdf", "phase_6h_pdf_checklist_upgrade")
            )
            
            has_substance = (
                "eligible" in passage_lower or
                "criteria" in passage_lower or
                "requirement" in passage_lower or
                "must have" in passage_lower or
                "need to" in passage_lower or
                len(passage) > 200
            )
            
            if is_checklist_only and not has_substance:
                ie_concerns.append({
                    "requirement": req,
                    "issue": "checklist_docs_only",
                    "evidence_basis": evidence_basis,
                    "passage_length": len(passage or ""),
                })
    
    return {
        "pathway_id": pathway_id,
        "ie_concerns": ie_concerns,
        "is_checklist_docs_only": len(ie_concerns) > 0,
    }


def audit_readiness(
    evidence_rows: list[dict[str, Any]],
    provisionally_ready: list[str],
    pathways_to_audit: list[str] | None = None,
) -> list[ReadinessReview]:
    """Audit readiness of provisionally ready pathways.
    
    Args:
        evidence_rows: Current evidence matrix.
        provisionally_ready: List of pathway IDs marked as provisionally ready.
        pathways_to_audit: Specific pathways to audit (default: AUDIT_PATHWAYS).
    
    Returns:
        List of ReadinessReview results.
    """
    audit_list = pathways_to_audit or AUDIT_PATHWAYS
    reviews: list[ReadinessReview] = []
    
    for pid in audit_list:
        was_ready = pid in provisionally_ready
        
        # Get country from evidence rows
        pathway_rows = [r for r in evidence_rows if r["pathway_id"] == pid]
        country = pathway_rows[0].get("country", "") if pathway_rows else ""
        
        # Check evidence quality
        quality = check_evidence_quality(evidence_rows, pid)
        
        # Special IE audit
        ie_audit = None
        if pid.startswith("ie_"):
            ie_audit = audit_ie_pathway(evidence_rows, pid)
        
        # Determine decision
        if not was_ready:
            decision = "not_applicable"
            rationale = "Pathway was not provisionally ready"
        elif quality["has_concerns"]:
            if ie_audit and ie_audit["is_checklist_docs_only"]:
                decision = "downgrade"
                rationale = (
                    f"IE pathway evidence is checklist-docs-only: "
                    f"{[c['requirement'] for c in ie_audit['ie_concerns']]}. "
                    f"Requires substantive eligibility/process evidence."
                )
            elif any("COMPLETE without document_id" in c for c in quality["concerns"]):
                decision = "downgrade"
                rationale = "COMPLETE cells lack document_id references"
            elif any("insufficient passage" in c for c in quality["concerns"]):
                decision = "requires_manual_review"
                rationale = "Evidence passages are too short for automated verification"
            else:
                decision = "requires_manual_review"
                rationale = f"Concerns found: {'; '.join(quality['concerns'][:3])}"
        else:
            decision = "retain"
            rationale = "All critical requirements COMPLETE with substantive evidence"
        
        reviews.append(ReadinessReview(
            pathway_id=pid,
            country=country,
            was_provisionally_ready=was_ready,
            decision=decision,
            rationale=rationale,
            evidence_summary=quality["summary"],
            concerns=quality["concerns"],
            human_verified=False,  # Never True
        ))
    
    return reviews


def write_readiness_csv(reviews: list[ReadinessReview], path: Path) -> None:
    """Write readiness review to CSV."""
    fieldnames = [
        "pathway_id", "country", "was_provisionally_ready", "decision",
        "rationale", "concerns", "evidence_summary", "reviewed_at", "human_verified"
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in reviews:
            writer.writerow({
                "pathway_id": r.pathway_id,
                "country": r.country,
                "was_provisionally_ready": r.was_provisionally_ready,
                "decision": r.decision,
                "rationale": r.rationale,
                "concerns": "; ".join(r.concerns),
                "evidence_summary": str(r.evidence_summary),
                "reviewed_at": r.reviewed_at,
                "human_verified": r.human_verified,
            })


def generate_human_review_packet(
    reviews: list[ReadinessReview],
    evidence_rows: list[dict[str, Any]],
) -> str:
    """Generate markdown packet for human review.
    
    Includes pathways that need manual review with their evidence.
    """
    lines = [
        "# Phase 6I Readiness Human Review Packet",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Summary",
        "",
    ]
    
    # Summary table
    lines.append("| Pathway | Decision | Concerns |")
    lines.append("|---------|----------|----------|")
    for r in reviews:
        concerns_short = "; ".join(r.concerns[:2]) if r.concerns else "None"
        lines.append(f"| {r.pathway_id} | {r.decision} | {concerns_short[:50]}{'...' if len(concerns_short) > 50 else ''} |")
    
    lines.append("")
    lines.append("## Pathways Requiring Review")
    lines.append("")
    
    for r in reviews:
        if r.decision in ("requires_manual_review", "downgrade"):
            lines.append(f"### {r.pathway_id} ({r.country})")
            lines.append("")
            lines.append(f"**Decision:** {r.decision}")
            lines.append(f"**Rationale:** {r.rationale}")
            lines.append("")
            lines.append("**Evidence Summary:**")
            for req, cls in r.evidence_summary.items():
                lines.append(f"- {req}: {cls}")
            lines.append("")
            
            if r.concerns:
                lines.append("**Concerns:**")
                for c in r.concerns:
                    lines.append(f"- {c}")
                lines.append("")
            
            # Include relevant evidence passages
            def get_cls(row: dict) -> str:
                return row.get("classification") or row.get("classification_6h", "UNEVALUATED")
            
            pathway_rows = [
                row for row in evidence_rows
                if row["pathway_id"] == r.pathway_id
                and row["entity_type"] == "true_pathway"
                and get_cls(row) == "COMPLETE"
            ]
            if pathway_rows:
                lines.append("**Evidence Passages:**")
                lines.append("")
                for row in pathway_rows:
                    lines.append(f"**{row['requirement']}** (doc {row.get('document_id', 'N/A')}):")
                    passage = row.get("passage", "")[:300]
                    lines.append(f"> {passage}{'...' if len(row.get('passage', '')) > 300 else ''}")
                    lines.append("")
    
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("**Note:** This packet is for human review only. Do not set human_verified=true without manual verification.")
    
    return "\n".join(lines)
