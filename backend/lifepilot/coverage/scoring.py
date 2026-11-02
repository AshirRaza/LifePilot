"""Transparent country/pathway scoring (UNEVALUATED does not inflate coverage)."""

from __future__ import annotations

from collections import defaultdict

from lifepilot.coverage.schema import (
    CountryScore,
    CoverageClass,
    PathwayRecord,
    PathwayStatus,
    RequirementCell,
)
from lifepilot.coverage.taxonomy import AUDIT_COUNTRIES, COUNTRY_PARENT_SLOTS


COVERED_CLASSES = frozenset({CoverageClass.COMPLETE, CoverageClass.PARTIAL})


def pathway_ready(pathway: PathwayRecord, cells: list[RequirementCell]) -> bool:
    """Verified pathway with all applicable critical fields COMPLETE."""
    if pathway.status != PathwayStatus.VERIFIED:
        return False
    critical = [c for c in cells if c.is_critical and c.applicable]
    if not critical:
        return False
    return all(c.coverage_class == CoverageClass.COMPLETE for c in critical)


def score_countries(
    pathways: list[PathwayRecord],
    cells: list[RequirementCell],
    *,
    country_notes: dict[str, list[str]] | None = None,
) -> list[CountryScore]:
    notes_map = country_notes or {}
    cells_by_pathway: dict[str, list[RequirementCell]] = defaultdict(list)
    for cell in cells:
        cells_by_pathway[cell.pathway_id].append(cell)

    scores: list[CountryScore] = []
    for country in AUDIT_COUNTRIES:
        country_pathways = [p for p in pathways if p.country == country]
        inventoried = [
            p
            for p in country_pathways
            if p.status != PathwayStatus.UNKNOWN_OR_NOT_INVENTORIED
        ]
        parent_slots = len(COUNTRY_PARENT_SLOTS.get(country, ()))
        with_evidence = [
            p for p in inventoried if p.document_ids and not p.evidence_thin
        ]
        # Also count thin-but-present as "any evidence"
        any_evidence = [p for p in inventoried if p.document_ids]
        verified = [p for p in inventoried if p.status == PathwayStatus.VERIFIED]

        country_cells = [
            c
            for p in country_pathways
            for c in cells_by_pathway.get(p.pathway_id, [])
            if c.applicable and p.status != PathwayStatus.UNKNOWN_OR_NOT_INVENTORIED
        ]

        def _count(cls: CoverageClass) -> int:
            return sum(1 for c in country_cells if c.coverage_class == cls)

        complete = _count(CoverageClass.COMPLETE)
        partial = _count(CoverageClass.PARTIAL)
        missing = _count(CoverageClass.MISSING)
        blocked = _count(CoverageClass.BLOCKED)
        unevaluated = _count(CoverageClass.UNEVALUATED)
        not_applicable = _count(CoverageClass.NOT_APPLICABLE)

        # Covered numerators exclude UNEVALUATED (and N/A)
        scored_cells = [
            c
            for c in country_cells
            if c.coverage_class
            not in {CoverageClass.UNEVALUATED, CoverageClass.NOT_APPLICABLE}
        ]
        covered = sum(1 for c in scored_cells if c.coverage_class in COVERED_CLASSES)
        covered_rate = (covered / len(scored_cells)) if scored_cells else 0.0

        critical_cells = [c for c in country_cells if c.is_critical and c.applicable]
        # Exclude UNEVALUATED from critical denominator inflation
        critical_scored = [
            c
            for c in critical_cells
            if c.coverage_class != CoverageClass.UNEVALUATED
        ]
        critical_complete = sum(
            1 for c in critical_scored if c.coverage_class == CoverageClass.COMPLETE
        )
        critical_rate = (
            critical_complete / len(critical_scored) if critical_scored else 0.0
        )

        ready = sum(
            1
            for p in inventoried
            if pathway_ready(p, cells_by_pathway.get(p.pathway_id, []))
        )

        scores.append(
            CountryScore(
                country=country,
                parent_slots_considered=parent_slots,
                pathways_inventoried=len(inventoried),
                pathways_with_any_evidence=len(any_evidence),
                pathways_verified=len(verified),
                pathways_ready=ready,
                requirement_cells_total=len(country_cells),
                requirement_cells_complete=complete,
                requirement_cells_partial=partial,
                requirement_cells_missing=missing,
                requirement_cells_blocked=blocked,
                requirement_cells_unevaluated=unevaluated,
                requirement_cells_not_applicable=not_applicable,
                critical_applicable_total=len(critical_scored),
                critical_complete=critical_complete,
                covered_rate=round(covered_rate, 4),
                critical_complete_rate=round(critical_rate, 4),
                notes=list(notes_map.get(country, [])),
            )
        )
    return scores
