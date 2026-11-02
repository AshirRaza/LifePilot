"""PostgreSQL vs offline Phase 6E.1 reconciliation (read-only)."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifepilot.coverage.normalization.run import run_calibration
from lifepilot.db.session import get_session_factory


def check_db_available() -> tuple[bool, str]:
    try:
        session = get_session_factory()()
        try:
            session.execute(text("SELECT 1")).scalar()
        finally:
            session.close()
        return True, "PostgreSQL connection succeeded"
    except Exception as exc:  # noqa: BLE001
        return False, f"{exc.__class__.__name__}: {exc}"


def _file_fingerprint(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def inspect_nl_pdfs(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT d.id AS document_id, d.language AS doc_language,
                   d.canonical_url, dv.id AS version_id, dv.language AS version_language,
                   dv.source_url, de.title, de.storage_path
            FROM documents d
            JOIN document_versions dv ON dv.document_id=d.id AND dv.is_current
            LEFT JOIN document_extractions de ON de.document_version_id=dv.id
            WHERE d.id IN (2249, 2250)
            ORDER BY d.id
            """
        )
    ).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "document_id": row["document_id"],
                "version_id": row["version_id"],
                "doc_language": row["doc_language"],
                "version_language": row["version_language"],
                "canonical_url": row["canonical_url"],
                "source_url": row["source_url"],
                "title": row["title"],
                "mismatch": (
                    (row["doc_language"] or "").lower() == "en"
                    and (row["version_language"] or "").lower() == "nl"
                ),
                "note": (
                    "Dutch content under /en/ URL path; do not rewrite historical versions"
                ),
            }
        )
    return out


def inspect_au_gte(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT d.id AS document_id, d.canonical_url, dv.id AS version_id,
                   dv.source_url
            FROM documents d
            JOIN document_versions dv ON dv.document_id=d.id AND dv.is_current
            WHERE d.primary_source_id = 11023
            ORDER BY d.id
            """
        )
    ).mappings().all()
    gte_rows = []
    genuine_student = []
    for row in rows:
        blob = f"{row['canonical_url']} {row['source_url']}".lower()
        item = {
            "document_id": row["document_id"],
            "version_id": row["version_id"],
            "canonical_url": row["canonical_url"],
            "source_url": row["source_url"],
        }
        if "genuine-temporary-entrant" in blob or "genuine temporary entrant" in blob:
            gte_rows.append(item)
        if "genuine-student" in blob or "genuine student" in blob:
            genuine_student.append(item)
    divergences = []
    for row in rows:
        can = (row["canonical_url"] or "").lower()
        src = (row["source_url"] or "").lower()
        if "genuine-temporary-entrant" in can and "genuine-student" in src:
            divergences.append(
                {
                    "document_id": row["document_id"],
                    "version_id": row["version_id"],
                    "canonical_url": row["canonical_url"],
                    "source_url": row["source_url"],
                    "observation": (
                        "canonical_url retains legacy GTE path while source_url points at "
                        "genuine-student-requirement; no final_url column to record redirect hop"
                    ),
                }
            )

    return {
        "gte_or_legacy_urls": gte_rows,
        "genuine_student_urls": genuine_student,
        "canonical_vs_source_divergences": divergences,
        "final_url_field_present": False,
        "redirect_proven": False,
        "conclusion": (
            "Partial redirect signal exists for document 2239: canonical_url uses legacy "
            "genuine-temporary-entrant path while source_url uses genuine-student-requirement. "
            "Without a final_url/redirect_observations field, full redirect provenance is not "
            "established. Do not rewrite historical versions; propose additive metadata later."
        ),
    }


def compare_matrices(
    offline_csv: Path,
    live_csv: Path,
) -> dict[str, Any]:
    import csv

    def load(path: Path) -> dict[tuple[str, str], dict[str, str]]:
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        key_pid = "original_pathway_id" if rows and "original_pathway_id" in rows[0] else "pathway_id"
        return {
            (r[key_pid], r["requirement_field"]): r for r in rows
        }

    offline = load(offline_csv)
    live = load(live_csv)
    class_diffs: list[dict[str, str]] = []
    only_offline = sorted(set(offline) - set(live))
    only_live = sorted(set(live) - set(offline))
    for key in sorted(set(offline) & set(live)):
        o, l = offline[key], live[key]
        if o.get("coverage_class") != l.get("coverage_class"):
            class_diffs.append(
                {
                    "pathway_id": key[0],
                    "requirement_field": key[1],
                    "offline_class": o.get("coverage_class", ""),
                    "live_class": l.get("coverage_class", ""),
                }
            )
    return {
        "offline_cells": len(offline),
        "live_cells": len(live),
        "coverage_class_differences": class_diffs,
        "only_in_offline": [f"{a}|{b}" for a, b in only_offline[:50]],
        "only_in_live": [f"{a}|{b}" for a, b in only_live[:50]],
        "class_diff_count": len(class_diffs),
    }


def run_live_calibration_compare(
    *,
    runs_dir: Path,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Rerun 6E.1 into a temp directory; never overwrite existing phase_6e1_*."""
    offline_matrix = runs_dir / "phase_6e1_calibrated_coverage_matrix.csv"
    offline_fp = _file_fingerprint(offline_matrix)

    with tempfile.TemporaryDirectory(prefix="lifepilot_6f0_") as tmp:
        tmp_path = Path(tmp)
        # Copy Phase 6E inputs only (required by calibration)
        for name in (
            "phase_6e_coverage_inventory.json",
            "phase_6e_coverage_matrix.csv",
            "phase_6e_gap_register.csv",
        ):
            src = runs_dir / name
            if not src.is_file():
                raise FileNotFoundError(f"Missing required Phase 6E artifact: {src}")
            shutil.copy2(src, tmp_path / name)

        live_paths = run_calibration(
            runs_dir=tmp_path,
            data_dir=data_dir,
            offline=False,
        )
        live_matrix = live_paths["matrix"]
        comparison = compare_matrices(offline_matrix, live_matrix)
        live_fp = _file_fingerprint(live_matrix)
        # Capture normalized counts
        norm = json.loads(
            (tmp_path / "phase_6e1_normalized_pathways.json").read_text(encoding="utf-8")
        )

    return {
        "live_calibration_ran": True,
        "wrote_into_temp_dir_only": True,
        "preserved_phase_6e1_artifacts": True,
        "offline_matrix_fingerprint": offline_fp,
        "live_matrix_fingerprint": live_fp,
        "matrices_identical": offline_fp == live_fp,
        "comparison": comparison,
        "live_normalized_counts": norm.get("counts"),
    }


def build_reconciliation_report(
    *,
    runs_dir: Path,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    available, message = check_db_available()
    report: dict[str, Any] = {
        "phase": "6F.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database_status": "AVAILABLE" if available else "BLOCKED",
        "database_message": message,
        "preserved_phase_6e_artifacts": True,
        "preserved_phase_6e1_artifacts": True,
        "commands_to_complete_later": [
            "python scripts/run_pathway_normalization.py",
            "python scripts/run_evidence_validation.py",
        ],
    }
    if not available:
        report["reconciliation"] = "BLOCKED"
        report["note"] = (
            "PostgreSQL unavailable. Evidence validation continued from offline "
            "artifacts; do not treat reconciliation as successful."
        )
        return report

    session = get_session_factory()()
    try:
        report["nl_pdf_language"] = inspect_nl_pdfs(session)
        report["au_student_gte_redirect"] = inspect_au_gte(session)
        # Basic corpus counts
        counts = session.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM sources WHERE review_status='approved') AS approved_sources,
                  (SELECT count(*) FROM documents) AS documents,
                  (SELECT count(*) FROM document_versions WHERE is_current) AS current_versions,
                  (SELECT count(*) FROM document_extractions WHERE status='success') AS success_extractions
                """
            )
        ).mappings().one()
        report["corpus_counts"] = dict(counts)
    finally:
        session.close()

    try:
        report["live_vs_offline"] = run_live_calibration_compare(
            runs_dir=runs_dir, data_dir=data_dir
        )
        report["reconciliation"] = "COMPLETED"
    except Exception as exc:  # noqa: BLE001
        report["reconciliation"] = "PARTIAL"
        report["live_calibration_error"] = f"{exc.__class__.__name__}: {exc}"

    # Discrepancy summary
    discrepancies: list[str] = []
    for row in report.get("nl_pdf_language") or []:
        if row.get("mismatch"):
            discrepancies.append(
                f"doc {row['document_id']}: doc_language={row['doc_language']} "
                f"version_language={row['version_language']} under {row['canonical_url']}"
            )
    gte = report.get("au_student_gte_redirect") or {}
    if gte and not gte.get("redirect_proven"):
        discrepancies.append("AU Student 500 GTE→Genuine Student redirect not proven from stored fields")
    live = report.get("live_vs_offline") or {}
    if live.get("comparison", {}).get("class_diff_count"):
        discrepancies.append(
            f"{live['comparison']['class_diff_count']} coverage_class differences "
            "between offline and live 6E.1 matrices"
        )
    report["discrepancies"] = discrepancies
    report["discrepancy_count"] = len(discrepancies)
    return report
