"""CLI entry for the Phase 6E read-only coverage audit."""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy.orm import Session

from lifepilot.coverage.classify import classify_pathways
from lifepilot.coverage.gaps import build_gap_register
from lifepilot.coverage.inventory import build_inventory
from lifepilot.coverage.report import (
    default_output_paths,
    write_acceptance_readiness,
    write_country_summary_md,
    write_gap_csv,
    write_inventory,
    write_matrix_csv,
)
from lifepilot.coverage.scoring import score_countries
from lifepilot.crawl.storage import data_root
from lifepilot.db.session import get_session_factory


def run_audit(
    session: Session,
    *,
    runs_dir: Path | None = None,
    data_dir: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Path]:
    root = repo_root or Path(__file__).resolve().parents[3]
    out_dir = runs_dir or (root / "data" / "runs")
    paths = default_output_paths(out_dir)

    bundle = build_inventory(
        session,
        data_dir=data_dir or data_root(),
        repo_root=root,
    )
    cells = classify_pathways(bundle.pathways, bundle.extractions)
    scores = score_countries(
        bundle.pathways,
        cells,
        country_notes=bundle.country_notes,
    )
    gaps = build_gap_register(
        bundle.pathways,
        cells,
        oos_leads=bundle.oos_leads,
    )

    write_inventory(
        paths["inventory"],
        pathways=bundle.pathways,
        sources_by_country=bundle.sources_by_country,
        country_notes=bundle.country_notes,
        frontier_pending=bundle.frontier_pending_by_country,
        oos_lead_count=len(bundle.oos_leads),
    )
    write_matrix_csv(paths["matrix"], cells)
    write_gap_csv(paths["gaps"], gaps)
    write_country_summary_md(paths["summary"], scores)
    write_acceptance_readiness(
        paths["acceptance"],
        scores=scores,
        pathways=bundle.pathways,
        cells=cells,
        gap_count=len(gaps),
    )
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 6E immigration knowledge coverage audit "
            "(read-only: Postgres + stored extractions; no HTTP)."
        )
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Output directory (default: data/runs)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Data root for extraction JSON (default: LIFEPILOT_DATA_DIR or data/)",
    )
    args = parser.parse_args(argv)

    session = get_session_factory()()
    try:
        paths = run_audit(
            session,
            runs_dir=args.runs_dir,
            data_dir=args.data_dir,
        )
    finally:
        session.close()

    print("Phase 6E coverage audit complete (read-only).")
    for key, path in paths.items():
        print(f"  {key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
