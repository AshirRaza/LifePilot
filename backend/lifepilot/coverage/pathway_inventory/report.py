"""Artifact writers and markdown report for Phase 6G."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def default_6g_paths(runs_dir: Path) -> dict[str, Path]:
    return {
        "baseline": runs_dir / "phase_6g_baseline.json",
        "pathway_inventory": runs_dir / "phase_6g_pathway_inventory.csv",
        "country_coverage": runs_dir / "phase_6g_country_coverage.csv",
        "source_discovery": runs_dir / "phase_6g_source_discovery.csv",
        "approval_queue": runs_dir / "phase_6g_approval_queue.csv",
        "ingestion_report": runs_dir / "phase_6g_ingestion_report.json",
        "evidence_coverage": runs_dir / "phase_6g_evidence_coverage.csv",
        "readiness": runs_dir / "phase_6g_readiness.json",
        "priority_backlog": runs_dir / "phase_6g_priority_backlog.csv",
        "report": runs_dir / "phase_6g_report.md",
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # Stable union of keys
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    k: (
                        json.dumps(v, ensure_ascii=False)
                        if isinstance(v, (list, dict))
                        else v
                    )
                    for k, v in row.items()
                }
            )


def write_report_md(
    path: Path,
    *,
    baseline: dict[str, Any],
    inventory_summary: dict[str, Any],
    country_rows: list[dict[str, Any]],
    ingestion: dict[str, Any],
    evidence_summary: dict[str, Any],
    readiness: dict[str, Any],
    approval_rows: list[dict[str, Any]],
    test_results: dict[str, Any],
    category_unknown: dict[str, Any],
) -> None:
    weak = sorted(
        country_rows,
        key=lambda r: (
            r["true_pathways_with_approved_source"],
            r["ingestion_coverage_rate"],
            r["evidence_complete_rate"],
        ),
    )[:5]
    pending = [
        a
        for a in approval_rows
        if str(a.get("approval_status", "")).startswith("pending")
    ]
    lines: list[str] = []
    lines.append("# Phase 6G — Systematic Immigration Pathway Coverage Expansion")
    lines.append("")
    lines.append(
        "Inventory-first expansion across 13 countries + EU. "
        "No RAG/APIs/frontend. Automated classifications remain provisional."
    )
    lines.append("")
    lines.append("## 1. Baseline versus final corpus")
    lines.append("")
    live0 = baseline.get("live_database") or {}
    after = (ingestion.get("after_counts") or live0)
    lines.append("| Metric | 6F.2 expected | Baseline (pre-write) | Final |")
    lines.append("|---|---:|---:|---:|")
    for key in (
        "approved_sources",
        "documents",
        "document_versions",
        "success_extractions",
    ):
        exp = (baseline.get("expected_from_6f2") or {}).get(key, "")
        lines.append(f"| {key} | {exp} | {live0.get(key, '')} | {after.get(key, '')} |")
    lines.append("")
    lines.append(
        f"Baseline captured_before_any_phase_writes="
        f"`{baseline.get('captured_before_any_phase_writes')}`"
    )
    lines.append(f"Ingestion delta: `{json.dumps(ingestion.get('delta') or {})}`")
    lines.append("")
    lines.append("## 2. Pathways inventoried")
    lines.append("")
    lines.append(f"- Total entities: **{inventory_summary.get('total_entities')}**")
    lines.append(f"- True pathways: **{inventory_summary.get('true_pathways')}**")
    lines.append(f"- Information hubs: **{inventory_summary.get('information_hubs')}**")
    lines.append(f"- Unknown candidates: **{inventory_summary.get('unknown_candidates')}**")
    lines.append(
        f"- True pathways with approved source link: "
        f"**{inventory_summary.get('true_pathways_with_approved_source')}**"
    )
    lines.append(
        f"- True pathways with ≥1 ingested document: "
        f"**{inventory_summary.get('true_pathways_with_ingested_docs')}**"
    )
    lines.append("")
    lines.append("### Per country (true pathways)")
    lines.append("")
    lines.append("| Country | True pathways | With approved source | With ingested docs |")
    lines.append("|---|---:|---:|---:|")
    for c, b in sorted((inventory_summary.get("by_country") or {}).items()):
        lines.append(
            f"| {c} | {b.get('true_pathways', 0)} | "
            f"{b.get('with_approved_source', 0)} | {b.get('with_ingested_docs', 0)} |"
        )
    lines.append("")
    lines.append("## 3. Unknown / uncovered categories")
    lines.append("")
    lines.append(
        f"- Unknown-candidate entities: **{category_unknown.get('unknown_candidate_entities')}**"
    )
    lines.append(
        "- NOT_OFFERED unused unless authoritative evidence exists; unknowns stay visible."
    )
    lines.append("")
    lines.append("## 4. Weakest country coverage")
    lines.append("")
    for row in weak:
        lines.append(
            f"- **{row['country']}**: approved_source_links="
            f"{row['true_pathways_with_approved_source']}/"
            f"{row['true_pathways_identified']}; "
            f"ingestion_rate={row['ingestion_coverage_rate']}; "
            f"evidence_complete_rate={row['evidence_complete_rate']} "
            f"({row['weakest_signal']})"
        )
    lines.append("")
    lines.append("## 5. Missing official sources")
    lines.append("")
    lines.append(
        f"- Approval-queue pending items: **{len(pending)}** "
        "(see `phase_6g_approval_queue.csv`)."
    )
    lines.append(
        "- Countries without any approved-source-linked true pathways typically include "
        "AE, JP, FR (and EU framework guidance relies on limited seeds)."
    )
    lines.append("")
    lines.append("## 6. New documents ingested")
    lines.append("")
    totals = ingestion.get("totals") or {}
    lines.append(
        f"- Fetch attempts: **{totals.get('fetches_attempted', 0)}** / "
        f"{(ingestion.get('limits') or {}).get('max_fetch_attempts', 100)}"
    )
    lines.append(f"- New document versions: **{totals.get('fetches_new_version', 0)}**")
    lines.append(f"- Failed: {totals.get('fetches_failed', 0)}; blocked: {totals.get('fetches_blocked', 0)}")
    lines.append(
        "- Held unapproved URLs were **not** fetched (IE checklists, IND 6009.pdf)."
    )
    lines.append("")
    lines.append("## 7. Evidence / readiness improvement")
    lines.append("")
    lines.append(f"- Evidence summary: `{json.dumps(evidence_summary)}`")
    lines.append(
        f"- Provisionally ready (matrix): "
        f"`{readiness.get('provisionally_ready_pathways')}`"
    )
    lines.append(
        f"- Human-verified: `{readiness.get('human_verified_ready_pathways')}`"
    )
    lines.append("")
    lines.append("## 8. Remaining unsupported critical requirements")
    lines.append("")
    for b in (readiness.get("top_blockers") or [])[:20]:
        lines.append(
            f"- `{b.get('pathway_id')}` / `{b.get('requirement')}` = {b.get('classification')}"
        )
    lines.append("")
    lines.append("## 9. URLs needing explicit approval")
    lines.append("")
    for a in pending[:25]:
        lines.append(
            f"- [{a.get('jurisdiction')}] `{a.get('pathway')}` — {a.get('url')} "
            f"({a.get('approval_status')})"
        )
    lines.append("")
    lines.append("## 10. Tests")
    lines.append("")
    lines.append(f"- Command: `{test_results.get('command')}`")
    lines.append(f"- Result: **{test_results.get('summary')}**")
    lines.append("")
    lines.append("## 11. Recommended next batch")
    lines.append("")
    lines.append("1. Approve AE / JP / FR official source candidates and seed allowlists.")
    lines.append("2. Approve exact IE checklist PDFs + IND `6009.pdf` allowlist entries.")
    lines.append("3. Deep-validate CA FSW/CEC/FST eligibility using already-ingested program pages.")
    lines.append("4. Expand DE Blue Card / skilled-worker allowlists beyond hubs.")
    lines.append("5. Human-verify the four provisionally ready pathways before production use.")
    lines.append("6. Do **not** start the next phase without approval.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "Historical Phase 6E/6F artifacts preserved. Immutable raw documents retained."
    )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
