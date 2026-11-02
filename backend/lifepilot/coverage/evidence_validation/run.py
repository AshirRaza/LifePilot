"""CLI entry for Phase 6F.0 evidence validation (read-only)."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from lifepilot.coverage.normalization.artifacts import default_runs_dir
from lifepilot.coverage.evidence_validation.readiness import recalculate_readiness
from lifepilot.coverage.evidence_validation.reconcile import build_reconciliation_report
from lifepilot.coverage.evidence_validation.remediation import build_remediation_plan
from lifepilot.coverage.evidence_validation.report import (
    default_6f0_paths,
    write_readiness,
    write_reconciliation,
    write_remediation,
    write_report_md,
)
from lifepilot.coverage.evidence_validation.review import build_evidence_review, write_review_csv
from lifepilot.crawl.storage import data_root


PRESERVED_ARTIFACTS = (
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
)


def fingerprint_artifacts(runs_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in PRESERVED_ARTIFACTS:
        path = runs_dir / name
        if path.is_file():
            out[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def assert_artifacts_unchanged(
    before: dict[str, str], after: dict[str, str]
) -> list[str]:
    problems: list[str] = []
    for name, digest in before.items():
        if after.get(name) != digest:
            problems.append(name)
    return problems


def run_phase_6f0(
    *,
    runs_dir: Path | None = None,
    data_dir: Path | None = None,
    skip_live_calibration: bool = False,
) -> dict[str, Path]:
    root_runs = runs_dir or default_runs_dir()
    paths = default_6f0_paths(root_runs)
    before = fingerprint_artifacts(root_runs)

    matrix = root_runs / "phase_6e1_calibrated_coverage_matrix.csv"
    if not matrix.is_file():
        raise FileNotFoundError(f"Missing Phase 6E.1 matrix: {matrix}")

    if skip_live_calibration:
        from lifepilot.coverage.evidence_validation.reconcile import check_db_available

        available, message = check_db_available()
        reconciliation = {
            "phase": "6F.0",
            "database_status": "AVAILABLE" if available else "BLOCKED",
            "database_message": message,
            "reconciliation": "SKIPPED_LIVE_CALIBRATION",
            "preserved_phase_6e_artifacts": True,
            "preserved_phase_6e1_artifacts": True,
            "discrepancies": [],
            "discrepancy_count": 0,
            "commands_to_complete_later": [
                "python scripts/run_evidence_validation.py",
            ],
            "note": "Live calibration compare skipped by flag.",
        }
        if available:
            from lifepilot.db.session import get_session_factory
            from lifepilot.coverage.evidence_validation.reconcile import (
                inspect_au_gte,
                inspect_nl_pdfs,
            )

            session = get_session_factory()()
            try:
                reconciliation["nl_pdf_language"] = inspect_nl_pdfs(session)
                reconciliation["au_student_gte_redirect"] = inspect_au_gte(session)
                discs = []
                for row in reconciliation["nl_pdf_language"]:
                    if row.get("mismatch"):
                        discs.append(
                            f"doc {row['document_id']}: language mismatch "
                            f"{row['doc_language']}/{row['version_language']}"
                        )
                if not reconciliation["au_student_gte_redirect"].get("redirect_proven"):
                    discs.append(
                        "AU Student 500 GTE→Genuine Student redirect not proven"
                    )
                reconciliation["discrepancies"] = discs
                reconciliation["discrepancy_count"] = len(discs)
                reconciliation["reconciliation"] = "COMPLETED_WITHOUT_MATRIX_RERUN"
            finally:
                session.close()
    else:
        reconciliation = build_reconciliation_report(
            runs_dir=root_runs, data_dir=data_dir or data_root()
        )

    decisions = build_evidence_review(matrix)
    readiness = recalculate_readiness(decisions)
    grounded = [d.source_url for d in decisions if d.source_url]
    remediation = build_remediation_plan(decisions, grounded_urls=grounded)

    write_review_csv(paths["review"], decisions)
    write_reconciliation(paths["reconciliation"], reconciliation)
    write_readiness(paths["readiness"], readiness)
    write_remediation(paths["remediation"], remediation)
    write_report_md(
        paths["report"],
        reconciliation=reconciliation,
        decisions=decisions,
        readiness=readiness,
        remediation=remediation,
    )

    after = fingerprint_artifacts(root_runs)
    changed = assert_artifacts_unchanged(before, after)
    if changed:
        raise RuntimeError(
            "Phase 6E/6E.1 artifacts were modified unexpectedly: "
            + ", ".join(changed)
        )

    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 6F.0 evidence validation and DB reconciliation "
            "(read-only; preserves Phase 6E/6E.1 artifacts)."
        )
    )
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument(
        "--skip-live-calibration",
        action="store_true",
        help="Skip temp-dir live 6E.1 rerun (still inspects NL/AU metadata if DB up).",
    )
    args = parser.parse_args(argv)
    paths = run_phase_6f0(
        runs_dir=args.runs_dir,
        data_dir=args.data_dir,
        skip_live_calibration=args.skip_live_calibration,
    )
    print("Phase 6F.0 evidence validation complete (read-only).")
    for key, path in paths.items():
        print(f"  {key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
