"""Prioritized gap register (no fabricated URLs)."""

from __future__ import annotations

from lifepilot.coverage.schema import (
    CoverageClass,
    GapRow,
    PathwayRecord,
    PathwayStatus,
    RemediationType,
    RequirementCell,
    Severity,
)
from lifepilot.coverage.taxonomy import CRITICAL_FIELDS


def _severity_for(
    cell: RequirementCell,
    pathway: PathwayRecord,
) -> Severity:
    if cell.coverage_class == CoverageClass.BLOCKED:
        return Severity.HIGH
    if cell.is_critical and cell.coverage_class in {
        CoverageClass.MISSING,
        CoverageClass.UNEVALUATED,
    }:
        return Severity.CRITICAL
    if cell.is_critical and cell.coverage_class == CoverageClass.PARTIAL:
        return Severity.HIGH
    if cell.coverage_class == CoverageClass.MISSING:
        return Severity.MEDIUM
    if cell.coverage_class == CoverageClass.PARTIAL:
        return Severity.LOW
    return Severity.MEDIUM


def _remediation(
    cell: RequirementCell,
    pathway: PathwayRecord,
) -> RemediationType:
    if cell.coverage_class == CoverageClass.BLOCKED:
        return RemediationType.BLOCKED_SOURCE
    if pathway.status == PathwayStatus.UNKNOWN_OR_NOT_INVENTORIED:
        return RemediationType.DISCOVERY
    if cell.needs_human_review or cell.coverage_class == CoverageClass.UNEVALUATED:
        return RemediationType.HUMAN_VERIFY
    if not pathway.document_ids:
        return RemediationType.DISCOVERY
    if cell.coverage_class in {CoverageClass.MISSING, CoverageClass.PARTIAL}:
        return RemediationType.EXTRACTION
    return RemediationType.ALLOWLIST_REVIEW


def _pick_lead(oos_leads: list[str], pathway: PathwayRecord) -> str | None:
    if pathway.country != "AU" or not oos_leads:
        return None
    # Prefer student-related leads for student pathway, else first lead
    if "student" in pathway.pathway_id or "student" in pathway.name.lower():
        for url in oos_leads:
            if "student" in url.lower() or "500" in url:
                return url
    return oos_leads[0] if oos_leads else None


def build_gap_register(
    pathways: list[PathwayRecord],
    cells: list[RequirementCell],
    *,
    oos_leads: list[str] | None = None,
) -> list[GapRow]:
    leads = oos_leads or []
    by_id = {p.pathway_id: p for p in pathways}
    gaps: list[GapRow] = []
    n = 0

    for cell in cells:
        pathway = by_id.get(cell.pathway_id)
        if pathway is None:
            continue
        if not cell.applicable:
            continue
        if cell.coverage_class in {
            CoverageClass.COMPLETE,
            CoverageClass.NOT_APPLICABLE,
        }:
            continue
        # Skip non-critical UNEVALUATED on unknown parent slots except one slot-level gap
        if (
            pathway.status == PathwayStatus.UNKNOWN_OR_NOT_INVENTORIED
            and cell.requirement_field != "eligibility"
        ):
            continue

        n += 1
        evidence = "; ".join(
            e.heading_or_excerpt[:80] for e in cell.evidence_refs[:3]
        )
        summary = (
            f"{pathway.name}: {cell.requirement_field} is {cell.coverage_class.value}"
        )
        if cell.notes:
            summary += f" — {cell.notes[0]}"

        gaps.append(
            GapRow(
                gap_id=f"G{n:04d}",
                country=pathway.country,
                pathway_id=pathway.pathway_id,
                requirement_field=cell.requirement_field,
                severity=_severity_for(cell, pathway),
                coverage_class=cell.coverage_class,
                summary=summary,
                source_ids=list(pathway.source_ids),
                document_ids=list(pathway.document_ids),
                remediation_type=_remediation(cell, pathway),
                existing_evidence=evidence,
                potential_lead_url=_pick_lead(leads, pathway)
                if pathway.country == "AU"
                and cell.requirement_field in CRITICAL_FIELDS
                else None,
            )
        )

    # Pathway-level blocked source gaps
    for pathway in pathways:
        if pathway.status != PathwayStatus.CANDIDATE:
            continue
        if not any("blocked" in n.lower() or "rejected" in n.lower() for n in pathway.notes):
            if set(pathway.source_ids) <= {87, 92} and pathway.source_ids:
                n += 1
                gaps.append(
                    GapRow(
                        gap_id=f"G{n:04d}",
                        country=pathway.country,
                        pathway_id=pathway.pathway_id,
                        requirement_field=None,
                        severity=Severity.HIGH,
                        coverage_class=CoverageClass.BLOCKED,
                        summary=f"{pathway.name}: source blocked/rejected (WAF)",
                        source_ids=list(pathway.source_ids),
                        document_ids=list(pathway.document_ids),
                        remediation_type=RemediationType.BLOCKED_SOURCE,
                        existing_evidence="",
                    )
                )

    severity_order = {
        Severity.CRITICAL: 0,
        Severity.HIGH: 1,
        Severity.MEDIUM: 2,
        Severity.LOW: 3,
    }
    gaps.sort(key=lambda g: (severity_order[g.severity], g.country, g.gap_id))
    return gaps
