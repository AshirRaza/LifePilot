"""Write Phase 6E JSON/CSV/MD deliverables."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lifepilot.coverage.schema import (
    CountryScore,
    GapRow,
    PathwayRecord,
    RequirementCell,
)
from lifepilot.coverage.taxonomy import AUDIT_COUNTRIES


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_inventory(
    path: Path,
    *,
    pathways: list[PathwayRecord],
    sources_by_country: dict[str, list[dict[str, Any]]],
    country_notes: dict[str, list[str]],
    frontier_pending: dict[str, int],
    oos_lead_count: int,
) -> None:
    payload = {
        "phase": "6E",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "countries": list(AUDIT_COUNTRIES),
        "pathways": [p.to_dict() for p in pathways],
        "approved_sources": {
            c: sources_by_country.get(c, []) for c in AUDIT_COUNTRIES
        },
        "country_notes": country_notes,
        "frontier_pending_by_country": frontier_pending,
        "au_oos_lead_count": oos_lead_count,
    }
    _write_json(path, payload)


def write_matrix_csv(path: Path, cells: list[RequirementCell]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "country",
        "pathway_id",
        "requirement_field",
        "coverage_class",
        "is_critical",
        "applicable",
        "needs_human_review",
        "evidence_count",
        "evidence_excerpt",
        "notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for cell in cells:
            excerpt = ""
            if cell.evidence_refs:
                excerpt = cell.evidence_refs[0].heading_or_excerpt[:160]
            writer.writerow(
                {
                    "country": cell.country,
                    "pathway_id": cell.pathway_id,
                    "requirement_field": cell.requirement_field,
                    "coverage_class": cell.coverage_class.value,
                    "is_critical": cell.is_critical,
                    "applicable": cell.applicable,
                    "needs_human_review": cell.needs_human_review,
                    "evidence_count": len(cell.evidence_refs),
                    "evidence_excerpt": excerpt,
                    "notes": " | ".join(cell.notes),
                }
            )


def write_gap_csv(path: Path, gaps: list[GapRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "gap_id",
        "country",
        "pathway_id",
        "requirement_field",
        "severity",
        "coverage_class",
        "summary",
        "source_ids",
        "document_ids",
        "remediation_type",
        "existing_evidence",
        "potential_lead_url",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for gap in gaps:
            writer.writerow(gap.to_dict())


def write_country_summary_md(path: Path, scores: list[CountryScore]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# Phase 6E — Country Coverage Summary",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "UNEVALUATED cells are excluded from covered/critical numerators.",
        "",
    ]
    for score in scores:
        lines.append(f"## {score.country}")
        lines.append("")
        lines.append(
            f"- Parent slots considered: **{score.parent_slots_considered}**"
        )
        lines.append(
            f"- Pathways inventoried: **{score.pathways_inventoried}** "
            f"(verified {score.pathways_verified}, with evidence "
            f"{score.pathways_with_any_evidence})"
        )
        lines.append(f"- Pathways ready (critical COMPLETE): **{score.pathways_ready}**")
        lines.append(
            f"- Requirement cells: complete={score.requirement_cells_complete}, "
            f"partial={score.requirement_cells_partial}, "
            f"missing={score.requirement_cells_missing}, "
            f"blocked={score.requirement_cells_blocked}, "
            f"unevaluated={score.requirement_cells_unevaluated}, "
            f"n/a={score.requirement_cells_not_applicable}"
        )
        lines.append(
            f"- Covered rate (COMPLETE+PARTIAL / scored): "
            f"**{score.covered_rate:.1%}**"
        )
        lines.append(
            f"- Critical COMPLETE rate: **{score.critical_complete_rate:.1%}** "
            f"({score.critical_complete}/{score.critical_applicable_total})"
        )
        if score.notes:
            lines.append("- Notes:")
            for note in score.notes:
                lines.append(f"  - {note}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_acceptance_readiness(
    path: Path,
    *,
    scores: list[CountryScore],
    pathways: list[PathwayRecord],
    cells: list[RequirementCell],
    gap_count: int,
) -> None:
    ready_countries = [
        s.country for s in scores if s.pathways_ready > 0 and s.pathways_inventoried > 0
    ]
    not_ready = [s.country for s in scores if s.country not in ready_countries]
    payload = {
        "phase": "6E",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "readiness_rule": (
            "Pathway ready = verified + all applicable critical fields COMPLETE. "
            "Country listed ready_partial if pathways_ready > 0."
        ),
        "critical_fields": [
            "eligibility",
            "required_documents",
            "application_process",
            "financial_or_sponsorship",
            "official_provenance",
        ],
        "countries_with_any_ready_pathway": ready_countries,
        "countries_without_ready_pathway": not_ready,
        "totals": {
            "pathways": len(pathways),
            "requirement_cells": len(cells),
            "gaps": gap_count,
            "pathways_ready": sum(s.pathways_ready for s in scores),
        },
        "per_country": [s.to_dict() for s in scores],
        "stop": {
            "remediation": False,
            "crawl": False,
            "approvals": False,
            "message": "Audit complete — awaiting approval before remediation",
        },
    }
    _write_json(path, payload)


def default_output_paths(runs_dir: Path) -> dict[str, Path]:
    return {
        "inventory": runs_dir / "phase_6e_coverage_inventory.json",
        "matrix": runs_dir / "phase_6e_coverage_matrix.csv",
        "gaps": runs_dir / "phase_6e_gap_register.csv",
        "summary": runs_dir / "phase_6e_country_summary.md",
        "acceptance": runs_dir / "phase_6e_acceptance_readiness.json",
    }
