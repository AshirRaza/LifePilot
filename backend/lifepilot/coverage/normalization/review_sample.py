"""Stratified human-review calibration sample (≥60 cells)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from lifepilot.coverage.normalization.denominators import PathwayReadiness
from lifepilot.coverage.normalization.normalize import NormalizedEntity
from lifepilot.coverage.schema import CoverageClass, RequirementCell

CRITICAL_FIELDS = (
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
)


@dataclass
class ReviewQueueItem:
    sample_id: str
    country: str
    original_pathway_id: str
    normalized_id: str
    entity_type: str
    requirement_field: str
    original_classification: str
    reviewed_classification: str
    provisional_classification: str
    document_id: str
    version_id: str
    exact_passage_or_heading: str
    reason_for_decision: str
    reviewer_status: str  # pending_human | automated_provisional
    stratum: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["notes"] = " | ".join(self.notes)
        return d


def _cell_key(cell: RequirementCell) -> tuple[str, str]:
    return (cell.pathway_id, cell.requirement_field)


def _first_passage(cell: RequirementCell) -> tuple[str, str, str]:
    if cell.evidence_refs:
        ref = cell.evidence_refs[0]
        return (
            str(ref.document_id),
            str(ref.version_id),
            ref.heading_or_excerpt[:400],
        )
    return ("", "", "")


def _provisional_from_automated(
    original: CoverageClass,
    calibrated: CoverageClass,
    *,
    entity_type: str,
) -> tuple[str, str]:
    """Return (provisional_class, reason). Never label as human verified."""
    if entity_type == "information_hub":
        return (
            calibrated.value,
            "Hub entity — provisional keep calibrated class; not a pathway readiness cell",
        )
    if entity_type == "unknown_slot":
        return (
            CoverageClass.UNEVALUATED.value,
            "Unknown slot — provisional UNEVALUATED pending inventory discovery",
        )
    if calibrated != original:
        return (
            calibrated.value,
            f"Automated relevance calibration changed {original.value}→{calibrated.value}",
        )
    if calibrated == CoverageClass.COMPLETE:
        return (
            calibrated.value,
            "Automated provisional: retained COMPLETE pending human spot-check",
        )
    return (
        calibrated.value,
        f"Automated provisional retains {calibrated.value}; human review required",
    )


def build_review_queue(
    *,
    entities: list[NormalizedEntity],
    original_cells: list[RequirementCell],
    calibrated_cells: list[RequirementCell],
    readiness: list[PathwayReadiness],
    min_size: int = 60,
) -> list[ReviewQueueItem]:
    by_entity = {e.original_pathway_id: e for e in entities}
    orig_map = {_cell_key(c): c for c in original_cells}
    cal_map = {_cell_key(c): c for c in calibrated_cells}
    ready_ids = {r.original_pathway_id for r in readiness if r.ready}
    # Phase 6E claimed ready set (may include hubs) — prioritize those originals
    phase6e_ready_priority = [
        "gb_skilled_worker",
        "nl_naturalisation",
        "nz_citizenship",
        "ie_coming_to_work",
        "ie_employment_permits",
    ]

    selected: list[tuple[str, str, str]] = []  # pathway_id, field, stratum
    seen: set[tuple[str, str]] = set()

    def add(pid: str, field: str, stratum: str) -> None:
        key = (pid, field)
        if key in seen:
            return
        if key not in orig_map and key not in cal_map:
            return
        seen.add(key)
        selected.append((pid, field, stratum))

    # 1) All five currently ready pathways — critical fields first
    for pid in phase6e_ready_priority:
        for field in CRITICAL_FIELDS:
            add(pid, field, "phase6e_ready_critical")

    # Also any newly ready true pathways
    for pid in sorted(ready_ids):
        for field in CRITICAL_FIELDS:
            add(pid, field, "calibrated_ready_critical")

    # 2) Singapore contradictions
    for pid in ("sg_employment_pass", "sg_permanent_resident"):
        for field in CRITICAL_FIELDS:
            add(pid, field, "singapore_readiness_contradiction")

    # 3) Canada Express Entry remaining critical gap
    for field in CRITICAL_FIELDS:
        cell = cal_map.get(("ca_express_entry", field)) or orig_map.get(
            ("ca_express_entry", field)
        )
        if cell and cell.coverage_class != CoverageClass.COMPLETE:
            add("ca_express_entry", field, "canada_express_entry_gap")
        elif field in CRITICAL_FIELDS:
            add("ca_express_entry", field, "canada_express_entry_gap")

    # 4) US / AU missing and partial
    for cell in calibrated_cells:
        ent = by_entity.get(cell.pathway_id)
        if not ent:
            continue
        if ent.country in {"US", "AU"} and cell.coverage_class in {
            CoverageClass.MISSING,
            CoverageClass.PARTIAL,
        }:
            if cell.is_critical or cell.requirement_field in CRITICAL_FIELDS:
                add(cell.pathway_id, cell.requirement_field, "us_au_missing_partial")

    # 5) Blocked Germany
    for cell in calibrated_cells:
        if cell.country == "DE" and cell.coverage_class == CoverageClass.BLOCKED:
            add(cell.pathway_id, cell.requirement_field, "germany_blocked")

    # 6) Unknown pathway categories (one eligibility cell per unknown)
    for ent in entities:
        if ent.entity_type == "unknown_slot":
            add(ent.original_pathway_id, "eligibility", "unknown_pathway_slot")

    # Top up with other incomplete critical pathway cells if needed
    if len(selected) < min_size:
        for cell in calibrated_cells:
            ent = by_entity.get(cell.pathway_id)
            if not ent or ent.entity_type != "pathway":
                continue
            if cell.is_critical and cell.coverage_class != CoverageClass.COMPLETE:
                add(cell.pathway_id, cell.requirement_field, "topup_incomplete_critical")
            if len(selected) >= min_size:
                break

    items: list[ReviewQueueItem] = []
    for i, (pid, field, stratum) in enumerate(selected, start=1):
        orig = orig_map.get((pid, field))
        cal = cal_map.get((pid, field)) or orig
        ent = by_entity.get(pid)
        if cal is None or ent is None:
            continue
        original_class = orig.coverage_class.value if orig else "ABSENT"
        provisional, reason = _provisional_from_automated(
            orig.coverage_class if orig else cal.coverage_class,
            cal.coverage_class,
            entity_type=ent.entity_type,
        )
        doc_id, ver_id, passage = _first_passage(cal)
        items.append(
            ReviewQueueItem(
                sample_id=f"R{i:03d}",
                country=ent.country,
                original_pathway_id=pid,
                normalized_id=ent.normalized_id,
                entity_type=ent.entity_type,
                requirement_field=field,
                original_classification=original_class,
                reviewed_classification="",  # human fills later
                provisional_classification=provisional,
                document_id=doc_id,
                version_id=ver_id,
                exact_passage_or_heading=passage,
                reason_for_decision=reason,
                reviewer_status="automated_provisional",
                stratum=stratum,
                notes=list(cal.notes),
            )
        )

    return items
