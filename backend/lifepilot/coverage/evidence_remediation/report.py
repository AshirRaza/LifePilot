"""Artifact writers for Phase 6F.1."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from lifepilot.coverage.evidence_remediation.assessments import FieldAssessment


def default_6f1_paths(runs_dir: Path) -> dict[str, Path]:
    return {
        "baseline": runs_dir / "phase_6f1_baseline.json",
        "evidence_repairs": runs_dir / "phase_6f1_evidence_repairs.csv",
        "extraction_repairs": runs_dir / "phase_6f1_extraction_repairs.json",
        "source_proposals": runs_dir / "phase_6f1_source_proposals.csv",
        "ingestion_report": runs_dir / "phase_6f1_ingestion_report.json",
        "readiness": runs_dir / "phase_6f1_readiness.json",
        "report": runs_dir / "phase_6f1_report.md",
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_assessments_csv(path: Path, assessments: list[FieldAssessment]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [a.to_row() for a in assessments]
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_proposals_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_report_md(
    path: Path,
    *,
    baseline: dict[str, Any],
    final_counts: dict[str, Any],
    assessments: list[FieldAssessment],
    readiness: dict[str, Any],
    extraction: dict[str, Any],
    ingestion: dict[str, Any],
    proposals: list[dict[str, Any]],
) -> None:
    promoted = readiness.get("fields_promoted_to_complete") or []
    fp_removed = readiness.get("false_positive_complete_removed") or []
    ready_before = readiness.get("provisional_ready_before_6f1") or []
    ready_after = readiness.get("provisional_ready_after_6f1") or []
    newly = readiness.get("newly_provisionally_ready") or []
    retained = readiness.get("previously_ready_retained") or []

    matching_fixes = [
        a
        for a in assessments
        if a.evidence_repair in {"corrected_match", "newly_linked", "normalized_split"}
    ]
    extract_fixes = [a for a in assessments if a.evidence_repair == "extraction_improved"]

    lines: list[str] = []
    lines.append("# Phase 6F.1 — Targeted Evidence Remediation Report")
    lines.append("")
    lines.append(
        "Bounded remediation after Phase 6F.0. Repair-first; no RAG/APIs/frontend. "
        "All classifications remain `automated_provisional`."
    )
    lines.append("")
    lines.append("## 1. Baseline and final database counts")
    lines.append("")
    live = baseline.get("live_database") or {}
    diffs = baseline.get("differences_from_expected") or {}
    lines.append("| Metric | Expected (6F.0 brief) | Live baseline | Final |")
    lines.append("|---|---:|---:|---:|")
    for key in (
        "approved_sources",
        "documents",
        "document_versions",
        "success_extractions",
    ):
        exp = (baseline.get("expected_from_6f0_brief") or {}).get(key, "")
        base_v = live.get(key, "")
        fin_v = (final_counts or {}).get(key, base_v)
        lines.append(f"| {key} | {exp} | {base_v} | {fin_v} |")
    lines.append("")
    if diffs:
        lines.append(f"Baseline differences from expected: `{json.dumps(diffs)}`")
    else:
        lines.append("Baseline matched the Phase 6F.0 brief counts exactly.")
    lines.append("")
    lines.append("## 2. Evidence classifications corrected")
    lines.append("")
    lines.append(
        f"- Fields assessed: **{len(assessments)}** "
        f"(counts: {json.dumps(readiness.get('classification_counts') or {})})"
    )
    lines.append(
        f"- Matching / linking repairs (`corrected_match` / `newly_linked` / "
        f"`normalized_split`): **{len(matching_fixes)}**"
    )
    lines.append(f"- Extraction-improved fields: **{len(extract_fixes)}**")
    lines.append("")
    lines.append("## 3. False-positive COMPLETE classifications removed")
    lines.append("")
    if not fp_removed:
        lines.append("None in this batch.")
    else:
        for row in fp_removed:
            lines.append(
                f"- `{row['pathway_id']}` / `{row['requirement']}`: "
                f"{row['from']} → {row['to']}"
            )
    lines.append("")
    lines.append("## 4. Previously incomplete fields promoted with valid evidence")
    lines.append("")
    if not promoted:
        lines.append("None.")
    else:
        for row in promoted:
            lines.append(
                f"- `{row['pathway_id']}` / `{row['requirement']}`: "
                f"{row['from']} → COMPLETE "
                f"(doc {row.get('document_id')}; repair={row.get('evidence_repair')})"
            )
    lines.append("")
    lines.append("## 5. Extraction improvements implemented")
    lines.append("")
    for imp in extraction.get("improvements") or []:
        lines.append(f"- **{imp.get('id')}** (`{imp.get('module')}` @ {imp.get('extractor_version')})")
        lines.append(f"  - {imp.get('change')}")
        result = imp.get("result") or {}
        lines.append(
            f"  - AU doc {result.get('document_id')}: "
            f"financial markers in raw={result.get('raw_markers')}; "
            f"fresh extract={result.get('fresh_extract_markers')}; "
            f"persist={result.get('persist_result')}"
        )
    lines.append("")
    lines.append("## 6. New documents ingested")
    lines.append("")
    lines.append(
        f"- Fetch attempts: **{(ingestion.get('fetch_attempts') or 0)}** / "
        f"max {(ingestion.get('limits') or {}).get('max_fetch_attempts', 25)}"
    )
    lines.append(
        f"- Documents newly ingested: **{len(ingestion.get('ingested_documents') or [])}**"
    )
    lines.append(
        f"- Reason: {ingestion.get('summary') or 'No eligible approved in-scope URLs requiring fetch.'}"
    )
    lines.append("")
    lines.append("## 7. Proposed URLs awaiting approval")
    lines.append("")
    pending = [p for p in proposals if p.get("approval_status") == "pending_human_approval"]
    eligible = [p for p in proposals if p.get("approval_status") == "eligible_if_in_allowlist"]
    lines.append(f"- Pending human approval (out of allowlist or unknown child): **{len(pending)}**")
    lines.append(f"- Eligible if confirmed in allowlist / existing corpus inspection: **{len(eligible)}**")
    for p in proposals:
        lines.append(
            f"- `{p.get('pathway_id')}` / `{p.get('requirement')}` — "
            f"{p.get('url')} "
            f"(allowlist={p.get('within_existing_allowlist')}; "
            f"status={p.get('approval_status')})"
        )
    lines.append("")
    lines.append("## 8. Provisionally ready pathways before and after")
    lines.append("")
    lines.append(f"- Before 6F.1: `{ready_before}`")
    lines.append(f"- After 6F.1: `{ready_after}`")
    lines.append(f"- Retained: `{retained}`")
    lines.append(f"- Newly provisionally ready: `{newly}`")
    lines.append("- Human-verified ready: `[]` (none assigned automatically)")
    lines.append("")
    lines.append("## 9. Remaining critical gaps for affected pathways")
    lines.append("")
    for block in readiness.get("pathway_blockers") or []:
        lines.append(f"### `{block['pathway_id']}`")
        if block.get("missing_critical_fields"):
            lines.append(f"- Missing critical field rows: {block['missing_critical_fields']}")
        for b in block.get("blockers") or []:
            lines.append(
                f"- `{b['requirement']}` = {b['classification']} "
                f"(was {b['previous_6f0']}; remediation={b['remediation']})"
            )
        lines.append("")
    lines.append("## 10. Recommended next remediation batch")
    lines.append("")
    lines.append("1. Approve ICA PR document checklist PDF allowlist amendment (exact URL recorded).")
    lines.append("2. Inspect/ingest DETE application-forms checklist children for IE CSEP/GEP.")
    lines.append("3. Locate IND permanent-residence supporting-document checklist child URL.")
    lines.append("4. Deepen NZ citizenship-by-grant eligibility beyond self-check teaser.")
    lines.append("5. Human-verify GB Skilled Worker + any newly provisional pathways before promotion.")
    lines.append("6. Do **not** start Phase 6F.2 without approval.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "Historical Phase 6E / 6E.1 / 6F.0 artifacts were not modified. "
        "Immutable raw documents and historical extractions preserved."
    )
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
