"""Artifact writers for Phase 6F.2."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from lifepilot.coverage.source_gap_closure.assessments import FieldAssessment


def default_6f2_paths(runs_dir: Path) -> dict[str, Path]:
    return {
        "baseline": runs_dir / "phase_6f2_baseline.json",
        "approved_ingestion": runs_dir / "phase_6f2_approved_ingestion.json",
        "evidence_repairs": runs_dir / "phase_6f2_evidence_repairs.csv",
        "source_proposals": runs_dir / "phase_6f2_source_proposals.csv",
        "readiness": runs_dir / "phase_6f2_readiness.json",
        "report": runs_dir / "phase_6f2_report.md",
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
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_proposals_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_report_md(
    path: Path,
    *,
    baseline: dict[str, Any],
    final_counts: dict[str, Any],
    assessments: list[FieldAssessment],
    readiness: dict[str, Any],
    ingestion: dict[str, Any],
    proposals: list[dict[str, Any]],
    test_results: dict[str, Any],
) -> None:
    promoted = readiness.get("fields_promoted_to_complete") or []
    downgraded = readiness.get("fields_downgraded_from_complete") or []
    unchanged = [
        a
        for a in assessments
        if a.evidence_repair == "unchanged" and a.classification == a.previous_6f1
    ]
    lines: list[str] = []
    lines.append("# Phase 6F.2 — Approved-Source Gap Closure Report")
    lines.append("")
    lines.append(
        "Bounded remediation after Phase 6F.1. Knowledge-base-first; no RAG/APIs/frontend. "
        "All classifications remain `automated_provisional`."
    )
    lines.append("")
    lines.append("## 1. Database counts before and after")
    lines.append("")
    live0 = baseline.get("live_database_at_baseline_call") or {}
    lines.append("| Metric | Expected (6F.1) | Baseline call | Final |")
    lines.append("|---|---:|---:|---:|")
    for key in (
        "approved_sources",
        "documents",
        "document_versions",
        "success_extractions",
    ):
        exp = (baseline.get("expected_from_6f1") or {}).get(key, "")
        lines.append(
            f"| {key} | {exp} | {live0.get(key, '')} | {(final_counts or {}).get(key, '')} |"
        )
    lines.append("")
    diffs = baseline.get("differences_from_6f1_expected") or {}
    if diffs:
        lines.append(f"Baseline differences vs 6F.1 expected: `{json.dumps(diffs)}`")
    else:
        lines.append("Baseline call matched 6F.1 expected counts (or only ICA delta after ingest).")
    lines.append("")
    lines.append("## 2. ICA checklist ingestion outcome")
    lines.append("")
    ica = (ingestion.get("ica_checklist") or {})
    lines.append(f"- Validation: `{json.dumps(ica.get('validation') or {})}`")
    lines.append(f"- Allowlist change: exact PDF URL only (no `/docs/` wildcard).")
    lines.append(
        f"- Fetch status: **{ica.get('fetch_status')}** "
        f"(document_id={ica.get('document_id')}, version_id={ica.get('version_id')})"
    )
    lines.append(
        f"- Extraction: status={ica.get('extraction_status')}, "
        f"blocks={ica.get('extraction_blocks')}"
    )
    lines.append(f"- Notes: {ica.get('notes')}")
    lines.append("")
    lines.append("## 3. New documents ingested")
    lines.append("")
    lines.append(
        f"- Fetch attempts: **{ingestion.get('fetch_attempts', 0)}** / "
        f"max {(ingestion.get('limits') or {}).get('max_fetch_attempts', 25)}"
    )
    for doc in ingestion.get("ingested_documents") or []:
        lines.append(
            f"- doc {doc.get('document_id')} — {doc.get('url')} "
            f"(sha256={str(doc.get('content_sha256') or '')[:16]}…)"
        )
    if not (ingestion.get("ingested_documents") or []):
        lines.append("- None beyond ICA if already counted above.")
    lines.append("")
    lines.append("## 4. Evidence fields promoted, downgraded, or unchanged")
    lines.append("")
    lines.append(f"- Fields assessed: **{len(assessments)}**")
    lines.append(f"- Classification counts: `{json.dumps(readiness.get('classification_counts') or {})}`")
    lines.append(f"- Promoted to COMPLETE: **{len(promoted)}**")
    for row in promoted:
        lines.append(
            f"  - `{row['pathway_id']}` / `{row['requirement']}`: "
            f"{row['from']} → COMPLETE (doc {row.get('document_id')}; {row.get('evidence_repair')})"
        )
    lines.append(f"- Downgraded from COMPLETE: **{len(downgraded)}**")
    for row in downgraded:
        lines.append(
            f"  - `{row['pathway_id']}` / `{row['requirement']}`: {row['from']} → {row['to']}"
        )
    lines.append(f"- Unchanged classifications: **{len(unchanged)}**")
    lines.append("")
    lines.append("## 5. Provisionally ready pathways before and after")
    lines.append("")
    lines.append(f"- Before: `{readiness.get('provisional_ready_before_6f2')}`")
    lines.append(f"- After: `{readiness.get('provisional_ready_after_6f2')}`")
    lines.append(f"- Retained: `{readiness.get('previously_ready_retained')}`")
    lines.append(f"- Newly ready: `{readiness.get('newly_provisionally_ready')}`")
    lines.append(f"- Downgraded from 6F.1 ready set: `{readiness.get('downgraded_from_6f1_ready')}`")
    lines.append("- Human-verified: `[]`")
    lines.append("")
    lines.append("## 6. Remaining critical blockers")
    lines.append("")
    for block in readiness.get("pathway_blockers") or []:
        lines.append(f"### `{block['pathway_id']}`")
        for b in block.get("blockers") or []:
            lines.append(
                f"- `{b['requirement']}` = {b['classification']} "
                f"(was {b['previous_6f1']}; remediation={b['remediation']})"
            )
        lines.append("")
    lines.append("## 7. Proposed URLs awaiting approval")
    lines.append("")
    for p in proposals:
        lines.append(
            f"- `{p.get('pathway_id')}` / `{p.get('requirement')}` — {p.get('url')} "
            f"(allowlist={p.get('within_existing_allowlist')}; status={p.get('approval_status')})"
        )
    lines.append("")
    lines.append("## 8. Tests executed and results")
    lines.append("")
    lines.append(f"- Command: `{test_results.get('command')}`")
    lines.append(f"- Result: **{test_results.get('summary')}**")
    if test_results.get("details"):
        lines.append(f"- Details: {test_results['details']}")
    lines.append("")
    lines.append("## 9. Concerns about overly broad pathway definitions")
    lines.append("")
    for c in readiness.get("pathway_normalization_concerns") or []:
        lines.append(f"- `{c.get('pathway_id')}`: {c.get('issue')} — {c.get('recommendation')}")
    lines.append("")
    lines.append("## 10. Recommended next batch")
    lines.append("")
    lines.append("1. Approve exact IE Critical Skills + General Employment Permit checklist PDF URLs.")
    lines.append("2. Approve exact IND form `6009.pdf` for permanent residence document evidence.")
    lines.append("3. Normalize `ca_express_entry` into FSW/CEC/FST pathway entities.")
    lines.append("4. Locate NZ citizenship-by-grant eligibility criteria beyond self-check.")
    lines.append("5. Human-verify newly provisional pathways before any verified promotion.")
    lines.append("6. Do **not** start the next phase without approval.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "Historical Phase 6E / 6E.1 / 6F.0 / 6F.1 artifacts were not modified. "
        "Immutable raw documents and historical extractions preserved."
    )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
