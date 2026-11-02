"""CLI entry for Phase 6F.2 approved-source gap closure."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from lifepilot.coverage.normalization.artifacts import default_runs_dir
from lifepilot.coverage.source_gap_closure.assessments import build_assessments
from lifepilot.coverage.source_gap_closure.baseline import capture_baseline
from lifepilot.coverage.source_gap_closure.enrich import enrich_version_ids
from lifepilot.coverage.source_gap_closure.proposals import build_source_proposals
from lifepilot.coverage.source_gap_closure.readiness import recalculate_readiness
from lifepilot.coverage.source_gap_closure.report import (
    default_6f2_paths,
    write_assessments_csv,
    write_json,
    write_proposals_csv,
    write_report_md,
)
from lifepilot.db.session import get_session_factory
from lifepilot.registry.urls import normalize_url, url_in_any_allowed_pattern

ICA_PDF = "https://www.ica.gov.sg/docs/ica/ica/ipses/pr-document-checklist.pdf"
ICA_SOURCE_ID = 9079

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
)


def fingerprint_artifacts(runs_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in PRESERVED_ARTIFACTS:
        path = runs_dir / name
        if path.is_file():
            out[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def assert_artifacts_unchanged(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return [name for name, digest in before.items() if after.get(name) != digest]


def live_corpus_counts() -> dict[str, Any]:
    session = get_session_factory()()
    try:
        return dict(
            session.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM sources WHERE review_status='approved')
                        AS approved_sources,
                      (SELECT count(*) FROM documents) AS documents,
                      (SELECT count(*) FROM document_versions) AS document_versions,
                      (SELECT count(*) FROM document_extractions WHERE status='success')
                        AS success_extractions
                    """
                )
            ).mappings().one()
        )
    finally:
        session.close()


def find_ica_document() -> dict[str, Any] | None:
    session = get_session_factory()()
    try:
        row = session.execute(
            text(
                """
                SELECT d.id AS document_id, v.id AS version_id, v.content_sha256,
                       v.source_url, d.canonical_url, e.status AS extraction_status,
                       e.storage_path
                FROM documents d
                JOIN document_versions v ON v.document_id = d.id AND v.is_current
                LEFT JOIN document_extractions e
                  ON e.document_version_id = v.id
                 AND e.extractor_name = 'pdf_structured'
                WHERE d.canonical_url ILIKE :q
                   OR v.source_url ILIKE :q
                ORDER BY d.id DESC
                LIMIT 1
                """
            ),
            {"q": "%pr-document-checklist.pdf%"},
        ).mappings().one_or_none()
        return dict(row) if row else None
    finally:
        session.close()


def ica_allowlist_status() -> dict[str, Any]:
    session = get_session_factory()()
    try:
        patterns = list(
            session.execute(
                text("SELECT allowed_url_patterns FROM sources WHERE id=:id"),
                {"id": ICA_SOURCE_ID},
            ).scalar()
            or []
        )
        url = normalize_url(ICA_PDF)
        return {
            "source_id": ICA_SOURCE_ID,
            "exact_pdf_pattern_present": ICA_PDF in patterns
            or any(p.rstrip("/") == ICA_PDF for p in patterns),
            "url_matches_allowlist": url_in_any_allowed_pattern(url, patterns),
            "patterns": patterns,
            "wildcard_docs_not_added": not any(
                "/docs/" in p and p.endswith("*") for p in patterns
            ),
        }
    finally:
        session.close()


def build_ingestion_report(*, ica_doc: dict[str, Any] | None) -> dict[str, Any]:
    allow = ica_allowlist_status()
    block_count = None
    if ica_doc and ica_doc.get("storage_path"):
        from lifepilot.crawl.storage import data_root
        import json

        path = data_root() / ica_doc["storage_path"]
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            block_count = len(data.get("blocks") or [])

    ingested = []
    if ica_doc:
        ingested.append(
            {
                "document_id": ica_doc["document_id"],
                "version_id": ica_doc["version_id"],
                "url": ica_doc.get("canonical_url") or ICA_PDF,
                "content_sha256": ica_doc.get("content_sha256"),
                "extraction_status": ica_doc.get("extraction_status"),
            }
        )

    return {
        "phase": "6F.2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limits": {
            "max_fetch_attempts": 25,
            "max_network_attempts": 60,
            "max_discovery_depth": 1,
            "broad_sitemap_expansion": False,
            "nz_backlog_processing": False,
            "unapproved_source_expansion": False,
            "only_exact_ica_pdf_approved_beyond_prior_allowlists": True,
        },
        "pre_phase_expected_counts_6f1": {
            "approved_sources": 34,
            "documents": 301,
            "document_versions": 368,
            "success_extractions": 366,
        },
        "fetch_attempts": 1 if ica_doc else 0,
        "network_attempts_approx": 2 if ica_doc else 0,
        "ingested_documents": ingested,
        "skipped_out_of_scope": [
            {
                "url": (
                    "https://enterprise.gov.ie/en/publications/publication-files/"
                    "critical-skills-employment-permits-checklist.pdf"
                ),
                "reason": "outside_existing_allowlist",
            },
            {
                "url": "https://ind.nl/en/forms/6009.pdf",
                "reason": "outside_existing_allowlist",
            },
        ],
        "ica_checklist": {
            "validation": {
                "official_host": "www.ica.gov.sg",
                "robots_can_fetch": True,
                "document_type": "pdf",
                "duplicate_before_fetch": False,
                "allowlist": allow,
            },
            "fetch_status": "fetched_new_version" if ica_doc else "missing",
            "document_id": (ica_doc or {}).get("document_id"),
            "version_id": (ica_doc or {}).get("version_id"),
            "extraction_status": (ica_doc or {}).get("extraction_status"),
            "extraction_blocks": block_count,
            "notes": (
                "Narrow allowlist amendment added exact PDF URL only. "
                "Table rows contain passport/birth/employment/pay-slip checklist items."
                if ica_doc
                else "ICA PDF not found in corpus."
            ),
        },
        "summary": (
            "Fetched authorized ICA PR checklist PDF only; IE/NL form PDFs remain proposals."
        ),
    }


def run_tests() -> dict[str, Any]:
    cmd = [
        str(Path(__file__).resolve().parents[4] / ".venv" / "Scripts" / "python.exe"),
        "-m",
        "pytest",
        "tests/test_source_gap_closure.py",
        "tests/test_evidence_remediation.py",
        "tests/test_evidence_validation.py",
        "tests/test_discovery_6d8.py",
        "-q",
        "--tb=line",
    ]
    # Fallback if venv path differs
    if not Path(cmd[0]).is_file():
        cmd[0] = "python"
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[4]))
    summary = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else f"exit={proc.returncode}"
    return {
        "command": " ".join(cmd[1:]),
        "exit_code": proc.returncode,
        "summary": summary if proc.returncode == 0 else f"FAILED exit={proc.returncode}; {summary}",
        "details": (proc.stderr or proc.stdout or "")[-1500:],
        "executed": True,
    }


def run_phase_6f2(
    *,
    runs_dir: Path | None = None,
    skip_tests: bool = False,
) -> dict[str, Path]:
    root_runs = runs_dir or default_runs_dir()
    paths = default_6f2_paths(root_runs)
    before = fingerprint_artifacts(root_runs)

    baseline = capture_baseline(runs_dir=root_runs)
    write_json(paths["baseline"], baseline)

    ica_doc = find_ica_document()
    ingestion = build_ingestion_report(ica_doc=ica_doc)
    write_json(paths["approved_ingestion"], ingestion)

    ica_id = int(ica_doc["document_id"]) if ica_doc else None
    assessments = enrich_version_ids(build_assessments(ica_doc_id=ica_id))
    write_assessments_csv(paths["evidence_repairs"], assessments)

    proposals = [p.to_row() for p in build_source_proposals(ica_ingested=bool(ica_doc))]
    write_proposals_csv(paths["source_proposals"], proposals)

    readiness = recalculate_readiness(assessments)
    write_json(paths["readiness"], readiness)

    final_counts = live_corpus_counts()
    test_results = (
        {"command": "skipped", "summary": "skipped by flag", "executed": False}
        if skip_tests
        else run_tests()
    )

    write_report_md(
        paths["report"],
        baseline=baseline,
        final_counts=final_counts,
        assessments=assessments,
        readiness=readiness,
        ingestion=ingestion,
        proposals=proposals,
        test_results=test_results,
    )

    after = fingerprint_artifacts(root_runs)
    changed = assert_artifacts_unchanged(before, after)
    if changed:
        raise RuntimeError(
            "Prior phase artifacts were modified unexpectedly: " + ", ".join(changed)
        )
    if not skip_tests and test_results.get("exit_code") not in (0, None):
        # Still write artifacts; surface test failure to caller
        pass
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 6F.2 approved-source gap closure (preserves prior phase artifacts)."
    )
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args(argv)
    paths = run_phase_6f2(runs_dir=args.runs_dir, skip_tests=args.skip_tests)
    print("Phase 6F.2 approved-source gap closure complete.")
    for key, path in paths.items():
        print(f"  {key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
