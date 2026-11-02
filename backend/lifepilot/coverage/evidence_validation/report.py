"""Write Phase 6F.0 JSON/MD deliverables."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lifepilot.coverage.evidence_validation.decisions import FieldDecision


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_reconciliation(path: Path, payload: dict[str, Any]) -> None:
    _write_json(path, payload)


def write_readiness(path: Path, payload: dict[str, Any]) -> None:
    _write_json(path, payload)


def write_remediation(path: Path, payload: dict[str, Any]) -> None:
    _write_json(path, payload)


def write_report_md(
    path: Path,
    *,
    reconciliation: dict[str, Any],
    decisions: list[FieldDecision],
    readiness: dict[str, Any],
    remediation: dict[str, Any],
) -> None:
    false_positives = [
        d
        for d in decisions
        if d.previous_classification == "COMPLETE"
        and d.proposed_classification != "COMPLETE"
    ]
    questionable = [
        d
        for d in decisions
        if d.evidence_quality_issue
        not in {"none", "matrix_carry_forward", "carried_forward_pending_deep_spotcheck"}
    ]
    matching_vs_missing = {
        "matching_problems": len(readiness.get("blockers_poor_evidence_matching") or []),
        "genuine_missing": len(readiness.get("blockers_genuine_missing_documents") or []),
        "extraction_provenance": len(
            readiness.get("blockers_extraction_or_provenance") or []
        ),
    }

    # Narrow retrieval prototype gate: need >=1 pathway with all critical COMPLETE
    # after provisional review AND official provenance COMPLETE.
    provisional_ready = readiness.get("previously_ready_still_provisional_ready") or []
    evidence_ready_for_narrow_prototype = len(provisional_ready) >= 1

    lines = [
        "# Phase 6F.0 — Evidence Validation & Database Reconciliation",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Phase 6E and 6E.1 artifacts were **not** modified.",
        "All classifications below are `automated_provisional` (not human-verified).",
        "",
        "## 1. Database reconciliation",
        "",
        f"- Status: **{reconciliation.get('database_status')}** / "
        f"reconciliation **{reconciliation.get('reconciliation')}**",
        f"- Message: {reconciliation.get('database_message')}",
        f"- Discrepancies: **{reconciliation.get('discrepancy_count', 0)}**",
    ]
    for disc in reconciliation.get("discrepancies") or []:
        lines.append(f"  - {disc}")
    if reconciliation.get("database_status") == "BLOCKED":
        lines.append("- Commands to complete later:")
        for cmd in reconciliation.get("commands_to_complete_later") or []:
            lines.append(f"  - `{cmd}`")
    lines.extend(
        [
            "",
            "## 2. Evidence fields inspected",
            "",
            f"- Review pack rows: **{len(decisions)}**",
            "",
            "## 3. Questionable evidence matches confirmed",
            "",
            f"- Rows with non-trivial quality issues: **{len(questionable)}**",
            "",
            "## 4. False-positive COMPLETE classifications",
            "",
            f"- Downgraded from COMPLETE: **{len(false_positives)}**",
        ]
    )
    for d in false_positives:
        lines.append(
            f"  - `{d.pathway_id}` / {d.requirement}: COMPLETE → "
            f"{d.proposed_classification} ({d.evidence_quality_issue})"
        )
    lines.extend(
        [
            "",
            "## 5. Revised provisional readiness",
            "",
            f"- Still provisionally ready: "
            f"**{readiness.get('previously_ready_still_provisional_ready')}**",
            f"- Downgraded former ready pathways: "
            f"**{len(readiness.get('previously_ready_downgraded') or [])}**",
            f"- Near-ready promotable after review: "
            f"**{readiness.get('near_ready_promotable_after_review')}**",
            "",
        ]
    )
    for item in readiness.get("previously_ready_downgraded") or []:
        lines.append(f"- Downgraded `{item['pathway_id']}` blockers:")
        for b in item.get("blockers") or []:
            lines.append(
                f"  - {b['requirement']}: {b['proposed']} ({b.get('issue')})"
            )
    lines.extend(
        [
            "",
            "## 6. Matching problems vs genuine gaps",
            "",
            f"- Poor evidence matching blockers: **{matching_vs_missing['matching_problems']}**",
            f"- Genuine missing-document blockers: **{matching_vs_missing['genuine_missing']}**",
            f"- Extraction/provenance blockers: **{matching_vs_missing['extraction_provenance']}**",
            "",
            "## 7. Top ten prioritized remediation actions",
            "",
        ]
    )
    for a in (remediation.get("top_10") or [])[:10]:
        lines.append(
            f"{a.get('rank')}. `{a['pathway_id']}` / {a['requirement']} — "
            f"{a['remediation_category']} → {a['proposed_classification']}"
        )
        if a.get("source_url"):
            lines.append(f"   - URL: {a['source_url']}")
    lines.extend(
        [
            "",
            "## 8. Narrow retrieval prototype?",
            "",
            f"- Evidence readiness for a **narrowly scoped** retrieval prototype: "
            f"**{'YES' if evidence_ready_for_narrow_prototype else 'NO'}**",
            f"- Pathways with provisional all-critical COMPLETE: {provisional_ready}",
            "",
            "This is **evidence readiness only**, not RAG system quality. Embeddings, "
            "retrieval, and answer generation have not been implemented. Do not treat "
            "provisional COMPLETE as human-verified production readiness.",
            "",
            "## Stop",
            "",
            "Awaiting approval before any Phase 6F ingestion, crawl, allowlist, or RAG work.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def default_6f0_paths(runs_dir: Path) -> dict[str, Path]:
    return {
        "review": runs_dir / "phase_6f0_evidence_review.csv",
        "reconciliation": runs_dir / "phase_6f0_reconciliation.json",
        "readiness": runs_dir / "phase_6f0_readiness.json",
        "remediation": runs_dir / "phase_6f0_remediation_plan.json",
        "report": runs_dir / "phase_6f0_report.md",
    }
