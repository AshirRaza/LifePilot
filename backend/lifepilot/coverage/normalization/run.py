"""CLI entry for Phase 6E.1 calibration (audit-only; no HTTP)."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from sqlalchemy.orm import Session

from lifepilot.coverage.inventory import load_corpus
from lifepilot.coverage.normalization.artifacts import (
    default_runs_dir,
    enrich_cells_with_corpus_refs,
    load_gap_rows,
    load_inventory,
    load_matrix_csv,
)
from lifepilot.coverage.normalization.denominators import (
    compute_all_readiness,
    group_cells_by_pathway,
)
from lifepilot.coverage.normalization.leads import (
    assess_gap_leads,
    invalid_or_generic_count,
)
from lifepilot.coverage.normalization.metadata import build_metadata_proposals
from lifepilot.coverage.normalization.normalize import normalize_inventory
from lifepilot.coverage.normalization.relevance import recalibrate_cell
from lifepilot.coverage.normalization.report import (
    default_6e1_paths,
    write_calibrated_matrix,
    write_corrected_gap_register,
    write_normalized_pathways,
    write_readiness_report,
    write_review_queue,
)
from lifepilot.coverage.normalization.review_sample import build_review_queue
from lifepilot.coverage.schema import RequirementCell
from lifepilot.coverage.taxonomy import is_critical_field
from lifepilot.crawl.storage import data_root
from lifepilot.db.session import get_session_factory


def _fix_critical_flags(
    cells: list[RequirementCell],
    entities_by_id: dict,
) -> list[RequirementCell]:
    out: list[RequirementCell] = []
    for cell in cells:
        ent = entities_by_id.get(cell.pathway_id)
        fin = ent.financial_applicable if ent else True
        applicable = cell.applicable
        if cell.requirement_field == "financial_or_sponsorship" and not fin:
            applicable = False
        out.append(
            RequirementCell(
                pathway_id=cell.pathway_id,
                country=cell.country,
                requirement_field=cell.requirement_field,
                coverage_class=cell.coverage_class,
                evidence_refs=list(cell.evidence_refs),
                needs_human_review=cell.needs_human_review,
                notes=list(cell.notes),
                is_critical=is_critical_field(
                    cell.requirement_field, financial_applicable=fin
                ),
                applicable=applicable,
            )
        )
    return out


def run_calibration(
    session: Session | None = None,
    *,
    runs_dir: Path | None = None,
    data_dir: Path | None = None,
    repo_root: Path | None = None,
    offline: bool = False,
) -> dict[str, Path]:
    root = repo_root or Path(__file__).resolve().parents[4]
    out_dir = runs_dir or default_runs_dir(root)
    paths_6e = {
        "inventory": out_dir / "phase_6e_coverage_inventory.json",
        "matrix": out_dir / "phase_6e_coverage_matrix.csv",
        "gaps": out_dir / "phase_6e_gap_register.csv",
    }
    for key, path in paths_6e.items():
        if not path.is_file():
            raise FileNotFoundError(f"Missing Phase 6E artifact ({key}): {path}")

    inventory = load_inventory(paths_6e["inventory"])
    original_cells = load_matrix_csv(paths_6e["matrix"])
    gap_rows = load_gap_rows(paths_6e["gaps"])

    entities, mapping = normalize_inventory(inventory)
    entities_by_id = {e.original_pathway_id: e for e in entities}
    original_to_normalized = {m["original_pathway_id"]: m["normalized_id"] for m in mapping}

    # Prefer DB corpus; fall back to stored extraction JSON (audit-only offline).
    from lifepilot.coverage.normalization.offline_corpus import load_corpus_from_disk

    views: list = []
    own_session = False
    db_session = session
    if offline:
        views = load_corpus_from_disk(entities, data_dir=data_dir or data_root())
    else:
        try:
            if db_session is None:
                db_session = get_session_factory()()
                own_session = True
            views = load_corpus(db_session, data_dir=data_dir or data_root())
        except Exception as exc:  # noqa: BLE001 — offline fallback is intentional
            print(
                f"DB corpus unavailable ({exc.__class__.__name__}); "
                "using offline extractions."
            )
            views = load_corpus_from_disk(entities, data_dir=data_dir or data_root())
        finally:
            if own_session and db_session is not None:
                db_session.close()

    views_by_doc = {v.document_id: v for v in views}
    views_by_source: dict[int, list] = {}
    for v in views:
        views_by_source.setdefault(v.source_id, []).append(v)

    doc_ids_by_pathway = {
        e.original_pathway_id: list(e.document_ids) for e in entities
    }
    version_by_doc = {v.document_id: v.version_id for v in views}
    extractor_by_doc = {v.document_id: v.extractor for v in views}

    enriched = enrich_cells_with_corpus_refs(
        original_cells,
        document_ids_by_pathway=doc_ids_by_pathway,
        version_by_document=version_by_doc,
        extractor_by_document=extractor_by_doc,
    )
    enriched = _fix_critical_flags(enriched, entities_by_id)

    calibrated: list[RequirementCell] = []
    questionable: list[str] = []
    for cell in enriched:
        ent = entities_by_id.get(cell.pathway_id)
        if ent is None:
            calibrated.append(cell)
            continue
        linked_views = []
        for doc_id in ent.document_ids:
            if doc_id in views_by_doc:
                linked_views.append(views_by_doc[doc_id])
        if not linked_views and ent.source_ids:
            # Prefer source-scoped views for single-pathway sources only
            if len([e for e in entities if set(e.source_ids) & set(ent.source_ids)]) == 1:
                for sid in ent.source_ids:
                    linked_views.extend(views_by_source.get(sid, []))
        new_cell, flags = recalibrate_cell(cell, ent, linked_views)
        calibrated.append(new_cell)
        questionable.extend(flags)

    calibrated = _fix_critical_flags(calibrated, entities_by_id)
    cells_by_pathway = group_cells_by_pathway(calibrated)
    readiness, countries = compute_all_readiness(entities, cells_by_pathway)

    lead_assessments = assess_gap_leads(
        gap_rows, original_to_normalized=original_to_normalized
    )
    invalid_leads = invalid_or_generic_count(lead_assessments)

    review_items = build_review_queue(
        entities=entities,
        original_cells=enriched,
        calibrated_cells=calibrated,
        readiness=readiness,
        min_size=60,
    )

    metadata_proposals = build_metadata_proposals(views)

    out_paths = default_6e1_paths(out_dir)
    write_normalized_pathways(
        out_paths["normalized"],
        entities=entities,
        mapping=mapping,
        metadata_proposals=metadata_proposals,
    )
    write_calibrated_matrix(out_paths["matrix"], calibrated, entities_by_id)
    write_review_queue(out_paths["review"], review_items)
    write_corrected_gap_register(
        out_paths["gaps"],
        gap_rows=gap_rows,
        entities_by_original=entities_by_id,
        lead_assessments=lead_assessments,
        calibrated_cells=calibrated,
    )

    # Top 20 priorities from corrected gap register (pathway only)
    with out_paths["gaps"].open(encoding="utf-8", newline="") as fh:
        gap_out = list(csv.DictReader(fh))
    priorities = [
        g
        for g in gap_out
        if g.get("include_in_pathway_priorities", "").lower() in {"true", "1", "yes"}
    ]
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    priorities.sort(
        key=lambda g: (
            sev_order.get(g.get("severity", "medium"), 9),
            g.get("country", ""),
            g.get("gap_id", ""),
        )
    )
    critical_gaps = sum(
        1
        for g in gap_out
        if g.get("entity_type") in {"pathway", "unknown_slot"}
        and g.get("severity") == "critical"
    )

    write_readiness_report(
        out_paths["report"],
        entities=entities,
        readiness=readiness,
        countries=countries,
        review_queue_size=len(review_items),
        questionable_evidence=len(questionable),
        invalid_leads=invalid_leads,
        critical_gaps=critical_gaps,
        top_priorities=priorities[:20],
        metadata_proposals=metadata_proposals,
    )
    return out_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 6E.1 coverage audit calibration "
            "(read-only; preserves Phase 6E artifacts)."
        )
    )
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip Postgres; use Phase 6E artifacts + data/extracted JSON only.",
    )
    args = parser.parse_args(argv)
    paths = run_calibration(
        runs_dir=args.runs_dir,
        data_dir=args.data_dir,
        offline=args.offline,
    )
    print("Phase 6E.1 calibration complete (audit-only).")
    for key, path in paths.items():
        print(f"  {key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
