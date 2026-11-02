"""Corrected readiness denominators for Phase 6E.1."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from lifepilot.coverage.normalization.normalize import NormalizedEntity
from lifepilot.coverage.schema import CoverageClass, RequirementCell
from lifepilot.coverage.taxonomy import AUDIT_COUNTRIES, COUNTRY_PARENT_SLOTS

NOT_ASSESSED = "NOT_ASSESSED"

INCOMPLETE_CRITICAL = frozenset(
    {
        CoverageClass.PARTIAL,
        CoverageClass.MISSING,
        CoverageClass.BLOCKED,
        CoverageClass.UNEVALUATED,
    }
)


@dataclass
class PathwayReadiness:
    original_pathway_id: str
    normalized_id: str
    country: str
    entity_type: str
    original_status: str
    ready: bool
    provisional_ready: bool
    critical_complete: int
    critical_incomplete: int
    critical_applicable_total: int
    blocking_fields: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CountryInventoryStatus:
    country: str
    parent_slots_considered: int
    true_pathways: int
    verified_pathways: int
    candidate_pathways: int
    information_hubs: int
    unknown_pathway_slots: int
    pathways_ready: int
    pathways_provisional_ready: int
    critical_fields_complete: int
    critical_fields_incomplete: int
    critical_fields_applicable_total: int
    inventory_completeness: str  # rate string or NOT_ASSESSED
    inventory_completeness_note: str
    national_coverage_claim: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def applicable_critical_cells(
    entity: NormalizedEntity,
    cells: list[RequirementCell],
) -> list[RequirementCell]:
    out: list[RequirementCell] = []
    for cell in cells:
        if not cell.applicable:
            continue
        if not cell.is_critical:
            continue
        if (
            cell.requirement_field == "financial_or_sponsorship"
            and not entity.financial_applicable
        ):
            continue
        out.append(cell)
    return out


def pathway_passes_critical(
    entity: NormalizedEntity,
    cells: list[RequirementCell],
    *,
    allow_provisional: bool = False,
) -> PathwayReadiness:
    """UNEVALUATED critical fields block readiness and remain in the denominator."""
    if entity.entity_type != "pathway":
        return PathwayReadiness(
            original_pathway_id=entity.original_pathway_id,
            normalized_id=entity.normalized_id,
            country=entity.country,
            entity_type=entity.entity_type,
            original_status=entity.original_status,
            ready=False,
            provisional_ready=False,
            critical_complete=0,
            critical_incomplete=0,
            critical_applicable_total=0,
            notes=["not a true pathway entity"],
        )

    crit = applicable_critical_cells(entity, cells)
    complete = [c for c in crit if c.coverage_class == CoverageClass.COMPLETE]
    incomplete = [c for c in crit if c.coverage_class in INCOMPLETE_CRITICAL]
    blocking = [
        f"{c.requirement_field}:{c.coverage_class.value}"
        for c in incomplete
    ]
    ready = bool(crit) and len(incomplete) == 0 and entity.original_status == "verified"
    provisional = False
    if (
        allow_provisional
        and not ready
        and entity.original_status == "verified"
        and crit
        and all(
            c.coverage_class in {CoverageClass.COMPLETE, CoverageClass.PARTIAL}
            for c in crit
        )
        and any(c.needs_human_review for c in crit)
    ):
        provisional = True

    return PathwayReadiness(
        original_pathway_id=entity.original_pathway_id,
        normalized_id=entity.normalized_id,
        country=entity.country,
        entity_type=entity.entity_type,
        original_status=entity.original_status,
        ready=ready,
        provisional_ready=provisional,
        critical_complete=len(complete),
        critical_incomplete=len(incomplete),
        critical_applicable_total=len(crit),
        blocking_fields=blocking,
        notes=[],
    )


def score_country_inventory(
    country: str,
    entities: list[NormalizedEntity],
    cells_by_original_id: dict[str, list[RequirementCell]],
    readiness: list[PathwayReadiness],
) -> CountryInventoryStatus:
    country_entities = [e for e in entities if e.country == country]
    pathways = [e for e in country_entities if e.entity_type == "pathway"]
    hubs = [e for e in country_entities if e.entity_type == "information_hub"]
    unknowns = [e for e in country_entities if e.entity_type == "unknown_slot"]
    verified = [e for e in pathways if e.original_status == "verified"]
    candidate = [e for e in pathways if e.original_status == "candidate"]

    parent_slots = len(COUNTRY_PARENT_SLOTS.get(country, ()))
    # Inventory completeness: share of parent slots that have ≥1 true pathway
    slots_with_pathway = {
        e.parent_category for e in pathways if e.parent_category
    }
    if parent_slots == 0 or (not pathways and not hubs and unknowns):
        # Zero-denominator / no pathway assessment
        completeness = NOT_ASSESSED
        completeness_note = (
            "No true pathways inventoried; display NOT_ASSESSED (never 0%)."
        )
    elif not pathways:
        completeness = NOT_ASSESSED
        completeness_note = (
            "Only hubs/unknown slots present; national pathway inventory not assessed."
        )
    else:
        rate = len(slots_with_pathway) / parent_slots if parent_slots else 0.0
        completeness = f"{rate:.1%}"
        completeness_note = (
            f"{len(slots_with_pathway)}/{parent_slots} parent slots have ≥1 true pathway. "
            "This is inventory slot fill, not national coverage."
        )

    ready_rows = [
        r
        for r in readiness
        if r.country == country and r.entity_type == "pathway"
    ]
    crit_complete = sum(r.critical_complete for r in ready_rows)
    crit_incomplete = sum(r.critical_incomplete for r in ready_rows)
    crit_total = sum(r.critical_applicable_total for r in ready_rows)

    return CountryInventoryStatus(
        country=country,
        parent_slots_considered=parent_slots,
        true_pathways=len(pathways),
        verified_pathways=len(verified),
        candidate_pathways=len(candidate),
        information_hubs=len(hubs),
        unknown_pathway_slots=len(unknowns),
        pathways_ready=sum(1 for r in ready_rows if r.ready),
        pathways_provisional_ready=sum(1 for r in ready_rows if r.provisional_ready),
        critical_fields_complete=crit_complete,
        critical_fields_incomplete=crit_incomplete,
        critical_fields_applicable_total=crit_total,
        inventory_completeness=completeness,
        inventory_completeness_note=completeness_note,
        national_coverage_claim=(
            "NOT claimed — inventoried pathways are not complete national coverage"
        ),
    )


def compute_all_readiness(
    entities: list[NormalizedEntity],
    cells_by_original_id: dict[str, list[RequirementCell]],
) -> tuple[list[PathwayReadiness], list[CountryInventoryStatus]]:
    readiness: list[PathwayReadiness] = []
    for entity in entities:
        cells = cells_by_original_id.get(entity.original_pathway_id, [])
        readiness.append(pathway_passes_critical(entity, cells))

    countries = [
        score_country_inventory(
            country,
            entities,
            cells_by_original_id,
            readiness,
        )
        for country in AUDIT_COUNTRIES
    ]
    return readiness, countries


def group_cells_by_pathway(
    cells: list[RequirementCell],
) -> dict[str, list[RequirementCell]]:
    out: dict[str, list[RequirementCell]] = defaultdict(list)
    for cell in cells:
        out[cell.pathway_id].append(cell)
    return out
