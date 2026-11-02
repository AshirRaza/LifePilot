"""CLI entry for Phase 6F.1 targeted evidence remediation."""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from lifepilot.coverage.normalization.artifacts import default_runs_dir
from lifepilot.coverage.evidence_remediation.assessments import build_assessments
from lifepilot.coverage.evidence_remediation.baseline import capture_baseline
from lifepilot.coverage.evidence_remediation.enrich import enrich_version_ids
from lifepilot.coverage.evidence_remediation.extraction import (
    build_extraction_repairs_report,
    verify_au_financial_in_raw_and_extract,
)
from lifepilot.coverage.evidence_remediation.proposals import build_source_proposals
from lifepilot.coverage.evidence_remediation.readiness import recalculate_readiness
from lifepilot.coverage.evidence_remediation.report import (
    default_6f1_paths,
    write_assessments_csv,
    write_json,
    write_proposals_csv,
    write_report_md,
)
from lifepilot.db.session import get_session_factory


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


def build_ingestion_report(*, skipped_reason: str) -> dict[str, Any]:
    return {
        "phase": "6F.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limits": {
            "max_fetch_attempts": 25,
            "max_network_attempts": 60,
            "max_discovery_depth": 1,
            "broad_sitemap_expansion": False,
            "nz_backlog_processing": False,
            "unapproved_source_expansion": False,
        },
        "fetch_attempts": 0,
        "network_attempts": 0,
        "ingested_documents": [],
        "skipped_out_of_scope": [
            {
                "url": (
                    "https://www.ica.gov.sg/docs/ica/ica/ipses/"
                    "pr-document-checklist.pdf?sfvrsn=ffa4df1c_0"
                ),
                "reason": "outside_existing_allowlist",
                "pathway_id": "sg_permanent_resident",
            }
        ],
        "summary": skipped_reason,
        "note": (
            "No unapproved fetches. Prefer repair of existing evidence over discovery. "
            "Useful missing URLs recorded in phase_6f1_source_proposals.csv."
        ),
    }


def run_evidence_remediation(
    *,
    runs_dir: Path | None = None,
    skip_au_reextract: bool = False,
) -> dict[str, Path]:
    root_runs = runs_dir or default_runs_dir()
    paths = default_6f1_paths(root_runs)
    before = fingerprint_artifacts(root_runs)

    baseline = capture_baseline(runs_dir=root_runs)
    write_json(paths["baseline"], baseline)

    au_result: dict[str, Any] | None = None
    if not skip_au_reextract:
        au_result = verify_au_financial_in_raw_and_extract()
    else:
        au_result = {
            "skipped": True,
            "au_financial_improved": False,
            "note": "AU re-extract skipped by flag",
        }

    extraction = build_extraction_repairs_report(au_result=au_result)
    write_json(paths["extraction_repairs"], extraction)

    au_improved = bool((au_result or {}).get("au_financial_improved"))
    assessments = build_assessments(au_financial_improved=au_improved)
    assessments = enrich_version_ids(assessments)
    write_assessments_csv(paths["evidence_repairs"], assessments)

    proposals = [p.to_row() for p in build_source_proposals()]
    write_proposals_csv(paths["source_proposals"], proposals)

    ingestion = build_ingestion_report(
        skipped_reason=(
            "All high-value missing URLs either require allowlist approval "
            "(ICA PR checklist PDF) or are better addressed by existing corpus "
            "inspection; no bounded fetch batch executed."
        )
    )
    write_json(paths["ingestion_report"], ingestion)

    readiness = recalculate_readiness(assessments)
    write_json(paths["readiness"], readiness)

    final_counts = live_corpus_counts()
    write_report_md(
        paths["report"],
        baseline=baseline,
        final_counts=final_counts,
        assessments=assessments,
        readiness=readiness,
        extraction=extraction,
        ingestion=ingestion,
        proposals=proposals,
    )

    after = fingerprint_artifacts(root_runs)
    changed = assert_artifacts_unchanged(before, after)
    if changed:
        raise RuntimeError(
            "Phase 6E/6E.1/6F.0 artifacts were modified unexpectedly: "
            + ", ".join(changed)
        )

    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 6F.1 targeted evidence remediation "
            "(repair-first; preserves Phase 6E/6E.1/6F.0 artifacts)."
        )
    )
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument(
        "--skip-au-reextract",
        action="store_true",
        help="Skip AU Student 500 re-extract verification against live DB.",
    )
    args = parser.parse_args(argv)
    paths = run_evidence_remediation(
        runs_dir=args.runs_dir,
        skip_au_reextract=args.skip_au_reextract,
    )
    print("Phase 6F.1 targeted evidence remediation complete.")
    for key, path in paths.items():
        print(f"  {key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
