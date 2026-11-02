"""Write Phase 6E.1 deliverables (do not modify Phase 6E artifacts)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lifepilot.coverage.normalization.denominators import (
    CountryInventoryStatus,
    PathwayReadiness,
)
from lifepilot.coverage.normalization.leads import LeadAssessment
from lifepilot.coverage.normalization.metadata import MetadataRemediationProposal
from lifepilot.coverage.normalization.normalize import NormalizedEntity
from lifepilot.coverage.normalization.review_sample import ReviewQueueItem
from lifepilot.coverage.schema import RequirementCell


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_normalized_pathways(
    path: Path,
    *,
    entities: list[NormalizedEntity],
    mapping: list[dict[str, str]],
    metadata_proposals: list[MetadataRemediationProposal],
) -> None:
    pathways = [e for e in entities if e.entity_type == "pathway"]
    hubs = [e for e in entities if e.entity_type == "information_hub"]
    unknowns = [e for e in entities if e.entity_type == "unknown_slot"]
    payload = {
        "phase": "6E.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "preserves_phase_6e_artifacts": True,
        "counts": {
            "true_pathways": len(pathways),
            "information_hubs": len(hubs),
            "unknown_slots": len(unknowns),
            "total_entities": len(entities),
        },
        "id_mapping": mapping,
        "entities": [e.to_dict() for e in entities],
        "metadata_remediation_proposals": [p.to_dict() for p in metadata_proposals],
    }
    _write_json(path, payload)


def write_calibrated_matrix(
    path: Path,
    cells: list[RequirementCell],
    entities_by_original: dict[str, NormalizedEntity],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "country",
        "original_pathway_id",
        "normalized_id",
        "entity_type",
        "requirement_field",
        "coverage_class",
        "is_critical",
        "applicable",
        "needs_human_review",
        "evidence_count",
        "evidence_excerpt",
        "document_id",
        "version_id",
        "notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for cell in cells:
            ent = entities_by_original.get(cell.pathway_id)
            excerpt = ""
            doc_id = ""
            ver_id = ""
            if cell.evidence_refs:
                excerpt = cell.evidence_refs[0].heading_or_excerpt[:200]
                doc_id = str(cell.evidence_refs[0].document_id or "")
                ver_id = str(cell.evidence_refs[0].version_id or "")
            writer.writerow(
                {
                    "country": cell.country,
                    "original_pathway_id": cell.pathway_id,
                    "normalized_id": ent.normalized_id if ent else cell.pathway_id,
                    "entity_type": ent.entity_type if ent else "",
                    "requirement_field": cell.requirement_field,
                    "coverage_class": cell.coverage_class.value,
                    "is_critical": cell.is_critical,
                    "applicable": cell.applicable,
                    "needs_human_review": cell.needs_human_review,
                    "evidence_count": len(cell.evidence_refs),
                    "evidence_excerpt": excerpt,
                    "document_id": doc_id,
                    "version_id": ver_id,
                    "notes": " | ".join(cell.notes),
                }
            )


def write_review_queue(path: Path, items: list[ReviewQueueItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "sample_id",
        "country",
        "original_pathway_id",
        "normalized_id",
        "entity_type",
        "requirement_field",
        "original_classification",
        "reviewed_classification",
        "provisional_classification",
        "document_id",
        "version_id",
        "exact_passage_or_heading",
        "reason_for_decision",
        "reviewer_status",
        "stratum",
        "notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            writer.writerow(item.to_dict())


def write_corrected_gap_register(
    path: Path,
    *,
    gap_rows: list[dict[str, str]],
    entities_by_original: dict[str, NormalizedEntity],
    lead_assessments: list[LeadAssessment],
    calibrated_cells: list[RequirementCell],
) -> None:
    """Rewrite gaps for true pathways + unknowns; attach lead validation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lead_by_gap = {a.gap_id: a for a in lead_assessments}
    cal_incomplete = {
        (c.pathway_id, c.requirement_field): c
        for c in calibrated_cells
        if c.applicable
        and c.is_critical
        and c.coverage_class.value
        in {"PARTIAL", "MISSING", "BLOCKED", "UNEVALUATED"}
    }

    fieldnames = [
        "gap_id",
        "country",
        "original_pathway_id",
        "normalized_id",
        "entity_type",
        "requirement_field",
        "severity",
        "coverage_class",
        "summary",
        "source_ids",
        "document_ids",
        "remediation_type",
        "existing_evidence",
        "potential_lead_url",
        "lead_class",
        "lead_validation_reason",
        "include_in_pathway_priorities",
    ]
    n = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        # Prefer calibrated critical incompletes for pathways/unknowns
        for (pid, field), cell in sorted(
            cal_incomplete.items(), key=lambda x: (x[0][0], x[0][1])
        ):
            ent = entities_by_original.get(pid)
            if ent is None:
                continue
            if ent.entity_type == "information_hub":
                continue
            n += 1
            severity = (
                "critical"
                if cell.coverage_class.value in {"MISSING", "UNEVALUATED", "BLOCKED"}
                else "high"
            )
            # Match original gap lead if any
            lead_url = ""
            lead_class = ""
            lead_reason = ""
            for row in gap_rows:
                if row.get("pathway_id") == pid and row.get("requirement_field") == field:
                    lead_url = row.get("potential_lead_url") or ""
                    assess = lead_by_gap.get(row.get("gap_id") or "")
                    if assess:
                        lead_class = assess.lead_class
                        lead_reason = assess.reason
                        if assess.lead_class in {
                            "unrelated_generic",
                            "unverified_lead",
                        }:
                            lead_url = ""  # do not recommend invalid URL
                    break
            writer.writerow(
                {
                    "gap_id": f"C{n:04d}",
                    "country": cell.country,
                    "original_pathway_id": pid,
                    "normalized_id": ent.normalized_id,
                    "entity_type": ent.entity_type,
                    "requirement_field": field,
                    "severity": severity,
                    "coverage_class": cell.coverage_class.value,
                    "summary": (
                        f"{ent.name}: {field} is {cell.coverage_class.value}"
                    ),
                    "source_ids": ",".join(str(s) for s in ent.source_ids),
                    "document_ids": ",".join(str(d) for d in ent.document_ids),
                    "remediation_type": (
                        "blocked_source"
                        if cell.coverage_class.value == "BLOCKED"
                        else "discovery"
                        if ent.entity_type == "unknown_slot"
                        else "extraction"
                    ),
                    "existing_evidence": (
                        cell.evidence_refs[0].heading_or_excerpt[:120]
                        if cell.evidence_refs
                        else ""
                    ),
                    "potential_lead_url": lead_url,
                    "lead_class": lead_class,
                    "lead_validation_reason": lead_reason,
                    "include_in_pathway_priorities": ent.entity_type == "pathway",
                }
            )


def write_readiness_report(
    path: Path,
    *,
    entities: list[NormalizedEntity],
    readiness: list[PathwayReadiness],
    countries: list[CountryInventoryStatus],
    review_queue_size: int,
    questionable_evidence: int,
    invalid_leads: int,
    critical_gaps: int,
    top_priorities: list[dict[str, Any]],
    metadata_proposals: list[MetadataRemediationProposal],
) -> None:
    pathways = [e for e in entities if e.entity_type == "pathway"]
    hubs = [e for e in entities if e.entity_type == "information_hub"]
    unknowns = [e for e in entities if e.entity_type == "unknown_slot"]
    ready = [r for r in readiness if r.ready]
    provisional = [r for r in readiness if r.provisional_ready]

    lines: list[str] = [
        "# Phase 6E.1 — Calibrated Coverage Readiness Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Original Phase 6E artifacts were **not** modified.",
        "",
        "## 1. Entity normalization",
        "",
        f"- True immigration pathways: **{len(pathways)}**",
        f"- Information hubs / cross-cutting resources: **{len(hubs)}**",
        f"- Unknown pathway slots: **{len(unknowns)}**",
        "",
        "## 2. Corrected ready-pathway count",
        "",
        f"- Pathways passing every applicable critical field: **{len(ready)}**",
        f"- Provisional ready (automated only, not human-verified): **{len(provisional)}**",
        "",
        "Ready pathway IDs:",
    ]
    for r in ready:
        lines.append(
            f"- `{r.normalized_id}` (original `{r.original_pathway_id}`)"
        )
    if not ready:
        lines.append("- _(none)_")
    lines.append("")
    lines.append("### Denominator rules")
    lines.append("")
    lines.append(
        "- UNEVALUATED critical fields block readiness and remain in the critical denominator."
    )
    lines.append(
        "- Information hubs are excluded from pathway readiness."
    )
    lines.append(
        "- Zero-denominator countries show `NOT_ASSESSED`, never 0% completeness."
    )
    lines.append(
        "- Inventoried pathways are **not** claimed as complete national coverage."
    )
    lines.append("")
    lines.append("## 3. Country-by-country inventory status")
    lines.append("")
    lines.append(
        "| Country | True pathways | Verified | Candidate | Hubs | Unknown | Ready | Inventory completeness |"
    )
    lines.append(
        "|---------|---------------|----------|-----------|------|---------|-------|------------------------|"
    )
    for c in countries:
        lines.append(
            f"| {c.country} | {c.true_pathways} | {c.verified_pathways} | "
            f"{c.candidate_pathways} | {c.information_hubs} | {c.unknown_pathway_slots} | "
            f"{c.pathways_ready} | {c.inventory_completeness} |"
        )
    lines.append("")
    lines.append("## 4. Critical gaps after normalization")
    lines.append("")
    lines.append(f"- Critical incomplete pathway/unknown cells: **{critical_gaps}**")
    lines.append("")
    lines.append("## 5. Questionable or unrelated evidence matches")
    lines.append("")
    lines.append(f"- Flagged during relevance filtering: **{questionable_evidence}**")
    lines.append("")
    lines.append("## 6. Invalid or generic remediation URLs")
    lines.append("")
    lines.append(f"- Invalid/generic/reused lead assessments: **{invalid_leads}**")
    lines.append("")
    lines.append("## 7. Human-review queue size")
    lines.append("")
    lines.append(
        f"- Review queue rows: **{review_queue_size}** "
        "(reviewer_status=`automated_provisional`; not human verified)"
    )
    lines.append("")
    lines.append("## 8. Top 20 evidence-backed remediation priorities")
    lines.append("")
    for i, item in enumerate(top_priorities[:20], start=1):
        lines.append(
            f"{i}. **{item.get('country')}** `{item.get('normalized_id')}` "
            f"{item.get('requirement_field')} — {item.get('coverage_class')} "
            f"({item.get('summary', '')[:120]})"
        )
    if not top_priorities:
        lines.append("_(none)_")
    lines.append("")
    lines.append("## Metadata remediation proposals (non-destructive)")
    lines.append("")
    for p in metadata_proposals:
        lines.append(f"- **{p.kind}**: {p.summary}")
        lines.append(f"  - Proposed: {p.proposed_action}")
    lines.append("")
    lines.append("## Stop")
    lines.append("")
    lines.append(
        "Awaiting approval. Do not begin Phase 6F, crawling, RAG, embeddings, agents, MCP, or frontend work."
    )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def default_6e1_paths(runs_dir: Path) -> dict[str, Path]:
    return {
        "normalized": runs_dir / "phase_6e1_normalized_pathways.json",
        "matrix": runs_dir / "phase_6e1_calibrated_coverage_matrix.csv",
        "review": runs_dir / "phase_6e1_review_queue.csv",
        "gaps": runs_dir / "phase_6e1_corrected_gap_register.csv",
        "report": runs_dir / "phase_6e1_readiness_report.md",
    }
