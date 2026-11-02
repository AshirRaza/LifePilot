"""CLI entry for Phase 6G systematic coverage expansion."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from lifepilot.coverage.normalization.artifacts import default_runs_dir
from lifepilot.coverage.pathway_inventory.coverage import (
    build_country_coverage,
    build_priority_backlog,
    category_unknown_counts,
)
from lifepilot.coverage.pathway_inventory.discovery import build_discovery_and_approval
from lifepilot.coverage.pathway_inventory.evidence import build_evidence_rows, evidence_summary
from lifepilot.coverage.pathway_inventory.ingest import run_bounded_ingestion
from lifepilot.coverage.pathway_inventory.inventory import build_inventory_rows, inventory_summary
from lifepilot.coverage.pathway_inventory.readiness import build_readiness
from lifepilot.coverage.pathway_inventory.report import (
    default_6g_paths,
    write_csv,
    write_json,
    write_report_md,
)

PRESERVED = (
    "phase_6e_coverage_inventory.json",
    "phase_6e_coverage_matrix.csv",
    "phase_6e_gap_register.csv",
    "phase_6e_country_summary.md",
    "phase_6e_acceptance_readiness.json",
    "phase_6e1_normalized_pathways.json",
    "phase_6e1_calibrated_coverage_matrix.csv",
    "phase_6e1_review_queue.csv",
    "phase_6e1_corrected_gap_register.csv",
    "phase_6e1_readiness_report.md",
    "phase_6f0_evidence_review.csv",
    "phase_6f0_reconciliation.json",
    "phase_6f0_readiness.json",
    "phase_6f0_remediation_plan.json",
    "phase_6f0_report.md",
    "phase_6f1_baseline.json",
    "phase_6f1_evidence_repairs.csv",
    "phase_6f1_extraction_repairs.json",
    "phase_6f1_source_proposals.csv",
    "phase_6f1_ingestion_report.json",
    "phase_6f1_readiness.json",
    "phase_6f1_report.md",
    "phase_6f2_baseline.json",
    "phase_6f2_approved_ingestion.json",
    "phase_6f2_evidence_repairs.csv",
    "phase_6f2_source_proposals.csv",
    "phase_6f2_readiness.json",
    "phase_6f2_report.md",
)


def fingerprint(runs_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in PRESERVED:
        path = runs_dir / name
        if path.is_file():
            out[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def run_tests() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[4]
    py = root / ".venv" / "Scripts" / "python.exe"
    cmd = [
        str(py if py.is_file() else "python"),
        "-m",
        "pytest",
        "tests/test_pathway_inventory.py",
        "tests/test_source_gap_closure.py",
        "tests/test_allowlist_exact_discovery.py",
        "-q",
        "--tb=line",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(root))
    summary = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else f"exit={proc.returncode}"
    return {
        "command": " ".join(cmd[1:]),
        "exit_code": proc.returncode,
        "summary": summary if proc.returncode == 0 else f"FAILED {summary}",
        "details": (proc.stdout or "")[-1200:],
        "executed": True,
    }


def run_pathway_inventory(
    *,
    runs_dir: Path | None = None,
    skip_ingestion: bool = False,
    skip_tests: bool = False,
    dry_run_ingestion: bool = False,
) -> dict[str, Path]:
    root_runs = runs_dir or default_runs_dir()
    paths = default_6g_paths(root_runs)
    before_fp = fingerprint(root_runs)

    # Baseline must already exist from pre-write capture; refuse to invent one after writes.
    if not paths["baseline"].is_file():
        raise FileNotFoundError(
            f"Missing pre-write baseline {paths['baseline']}. "
            "Capture baseline before ingestion."
        )
    baseline = json.loads(paths["baseline"].read_text(encoding="utf-8"))
    if not baseline.get("captured_before_any_phase_writes"):
        raise RuntimeError(
            "phase_6g_baseline.json missing captured_before_any_phase_writes=true"
        )

    # Inventory / discovery before ingestion (no DB writes yet beyond reads)
    inventory_rows = build_inventory_rows()
    inv_sum = inventory_summary(inventory_rows)
    write_csv(paths["pathway_inventory"], inventory_rows)

    discovery_rows, approval_rows = build_discovery_and_approval()
    write_csv(paths["source_discovery"], discovery_rows)
    write_csv(paths["approval_queue"], approval_rows)

    if skip_ingestion:
        ingestion = {
            "phase": "6G",
            "execute": False,
            "skipped": True,
            "before_counts": baseline.get("live_database"),
            "after_counts": baseline.get("live_database"),
            "delta": {
                "approved_sources": 0,
                "documents": 0,
                "document_versions": 0,
                "success_extractions": 0,
            },
            "totals": {"fetches_attempted": 0, "fetches_new_version": 0},
            "summary": "Ingestion skipped by flag",
        }
    else:
        ingestion = run_bounded_ingestion(execute=not dry_run_ingestion)
    write_json(paths["ingestion_report"], ingestion)

    # Rebuild inventory/evidence after ingestion so doc counts refresh
    inventory_rows = build_inventory_rows()
    inv_sum = inventory_summary(inventory_rows)
    write_csv(paths["pathway_inventory"], inventory_rows)

    evidence_rows = build_evidence_rows(ica_doc_id=2252)
    ev_sum = evidence_summary(evidence_rows)
    write_csv(paths["evidence_coverage"], evidence_rows)

    country_rows = build_country_coverage(inventory_rows, evidence_rows)
    write_csv(paths["country_coverage"], country_rows)

    backlog = build_priority_backlog(country_rows, approval_rows)
    write_csv(paths["priority_backlog"], backlog)

    readiness = build_readiness(
        evidence_rows, inventory_summary=inv_sum, ingestion=ingestion
    )
    write_json(paths["readiness"], readiness)

    cat_unknown = category_unknown_counts(inventory_rows)
    test_results = (
        {"command": "skipped", "summary": "skipped", "executed": False}
        if skip_tests
        else run_tests()
    )

    write_report_md(
        paths["report"],
        baseline=baseline,
        inventory_summary=inv_sum,
        country_rows=country_rows,
        ingestion=ingestion,
        evidence_summary=ev_sum,
        readiness=readiness,
        approval_rows=approval_rows,
        test_results=test_results,
        category_unknown=cat_unknown,
    )

    after_fp = fingerprint(root_runs)
    changed = [n for n, d in before_fp.items() if after_fp.get(n) != d]
    if changed:
        raise RuntimeError(
            "Prior phase artifacts modified unexpectedly: " + ", ".join(changed)
        )
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 6G systematic immigration pathway coverage expansion"
    )
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--skip-ingestion", action="store_true")
    parser.add_argument("--dry-run-ingestion", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args(argv)
    paths = run_pathway_inventory(
        runs_dir=args.runs_dir,
        skip_ingestion=args.skip_ingestion,
        dry_run_ingestion=args.dry_run_ingestion,
        skip_tests=args.skip_tests,
    )
    print("Phase 6G complete.")
    for k, p in paths.items():
        print(f"  {k}: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
