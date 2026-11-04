"""CLI for corpus reliability operations (backup, audit, review).

Historical one-shot expansion runners have been removed. Current corpus state
lives in data/runs/evidence_matrix.csv and coverage_matrix.csv.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

DATABASE_URL = os.environ.get("DATABASE_URL", "")

from lifepilot.coverage.corpus_reliability.assessment import (
    batch_assess,
    write_assessments_csv,
    write_changes_csv,
)
from lifepilot.coverage.corpus_reliability.backup import (
    BackupError,
    RestoreError,
    cleanup_restore_database,
    create_backup,
    get_corpus_counts,
    restore_data_files,
    restore_database,
    verify_backup,
)
from lifepilot.coverage.corpus_reliability.consistency import (
    audit_evidence_consistency,
    generate_repair_proposals,
    load_evidence_matrix,
    write_audit_csv,
    write_repairs_csv,
)
from lifepilot.coverage.corpus_reliability.discovery import (
    build_discovery_candidates,
    write_discovery_csv,
)
from lifepilot.coverage.corpus_reliability.human_review import (
    confirm_pathway,
    format_cell_for_review,
    record_decision,
    reviews_path,
)
from lifepilot.coverage.corpus_reliability.matrix_io import (
    BACKUP_MANIFEST_NAME,
    COVERAGE_MATRIX_NAME,
    EVIDENCE_MATRIX_NAME,
    coverage_snapshot,
    load_csv,
    verify_matrix_cardinality,
)
from lifepilot.coverage.corpus_reliability.readiness import (
    CRITICAL_REQUIREMENTS,
    audit_readiness,
    generate_human_review_packet,
    pathway_is_provisionally_ready,
    summarize_readiness,
    write_readiness_csv,
)
from lifepilot.db.session import get_session_factory

RUNS_DIR = ROOT / "data" / "runs"
BACKUPS_DIR = ROOT / "backups"
DATA_DIR = ROOT / "data"


def _resolve_evidence_csv() -> Path:
    preferred = RUNS_DIR / EVIDENCE_MATRIX_NAME
    if preferred.is_file():
        return preferred
    # Legacy fallbacks during transition
    for name in (
        "phase_6u_evidence_matrix.csv",
        "phase_6t_evidence_matrix.csv",
        "phase_6s_evidence_matrix.csv",
    ):
        path = RUNS_DIR / name
        if path.is_file():
            return path
    return preferred


def cmd_backup(args: argparse.Namespace) -> int:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is not set")
        return 1
    print("Creating backup...")
    print(f"  Backups dir: {BACKUPS_DIR}")
    try:
        result = create_backup(
            db_url=DATABASE_URL,
            data_dir=DATA_DIR,
            backups_dir=BACKUPS_DIR,
            include_extracted=not args.skip_extracted,
            include_compared=not args.skip_compared,
            include_runs=args.include_runs,
        )
    except BackupError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"\nBackup created: {result['backup_dir']}")
    manifest = result["manifest"]
    print(f"  Backup ID: {manifest['backup_id']}")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    manifest_ref = RUNS_DIR / BACKUP_MANIFEST_NAME
    manifest_ref.write_text(
        json.dumps({
            "backup_dir": result["backup_dir"],
            "backup_id": manifest["backup_id"],
            "manifest_path": result.get("manifest_path"),
        }, indent=2),
        encoding="utf-8",
    )
    print(f"Manifest reference: {manifest_ref}")
    return 0


def cmd_verify_backup(args: argparse.Namespace) -> int:
    backup_dir = Path(args.backup_dir) if args.backup_dir else None
    if not backup_dir:
        ref = RUNS_DIR / BACKUP_MANIFEST_NAME
        if ref.is_file():
            backup_dir = Path(json.loads(ref.read_text(encoding="utf-8"))["backup_dir"])
    if not backup_dir or not backup_dir.is_dir():
        print("ERROR: No backup directory specified and no recent backup found")
        return 1
    print(f"Verifying backup: {backup_dir}")
    result = verify_backup(backup_dir)
    print(f"\nBackup ID: {result['backup_id']}")
    print(f"Status: {result.get('status')}")
    if result.get("corpus_counts"):
        print("\nCorpus counts at backup time:")
        for k, v in result["corpus_counts"].items():
            print(f"    {k}: {v}")
    ok = result.get("status") == "ok"
    print("\nBackup verified OK" if ok else f"\nBackup verification FAILED: {result.get('issues')}")
    return 0 if ok else 1


def cmd_restore_rehearsal(args: argparse.Namespace) -> int:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is not set")
        return 1
    backup_dir = Path(args.backup_dir) if args.backup_dir else None
    if not backup_dir:
        ref = RUNS_DIR / BACKUP_MANIFEST_NAME
        if ref.is_file():
            backup_dir = Path(json.loads(ref.read_text(encoding="utf-8"))["backup_dir"])
    if not backup_dir or not backup_dir.is_dir():
        print("ERROR: No backup directory specified")
        return 1
    target_db = "lifepilot_restore_test"
    print(f"Restore rehearsal from: {backup_dir} -> {target_db}")
    try:
        restore_database(
            backup_dir=backup_dir,
            target_db_name=target_db,
            db_url=DATABASE_URL,
        )
        restore_data_files(
            backup_dir=backup_dir,
            target_dir=BACKUPS_DIR / "restore_rehearsal",
        )
        print("Restore rehearsal completed.")
        return 0
    except (BackupError, RestoreError) as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        try:
            cleanup_restore_database(db_name=target_db, db_url=DATABASE_URL)
        except Exception:
            pass


def cmd_consistency_audit(args: argparse.Namespace) -> int:
    evidence_csv = _resolve_evidence_csv()
    if not evidence_csv.is_file():
        print(f"ERROR: evidence matrix not found at {evidence_csv}")
        return 1
    rows = load_evidence_matrix(evidence_csv)
    factory = get_session_factory()
    with factory() as session:
        findings = audit_evidence_consistency(session, rows)
    repairs = generate_repair_proposals(findings)
    audit_path = RUNS_DIR / "consistency_audit.csv"
    repairs_path = RUNS_DIR / "consistency_repairs.csv"
    write_audit_csv(findings, audit_path)
    write_repairs_csv(repairs, repairs_path)
    print(f"Findings: {len(findings)}")
    print(f"Repairs: {len(repairs)}")
    print(f"Audit: {audit_path}")
    print(f"Repairs: {repairs_path}")
    return 0


def cmd_assess(args: argparse.Namespace) -> int:
    evidence_csv = _resolve_evidence_csv()
    if not evidence_csv.is_file():
        print(f"ERROR: evidence matrix not found at {evidence_csv}")
        return 1
    rows = load_csv(evidence_csv)
    factory = get_session_factory()
    with factory() as session:
        assessments, changes = batch_assess(
            session, rows, DATA_DIR, max_assessments=args.max_cells or 50,
        )
    assessments_path = RUNS_DIR / "evidence_assessments.csv"
    changes_path = RUNS_DIR / "evidence_changes.csv"
    write_assessments_csv(assessments, assessments_path)
    write_changes_csv(changes, changes_path)
    print(f"Assessments: {len(assessments)}")
    print(f"Changes: {len(changes)}")
    return 0


def cmd_readiness_audit(args: argparse.Namespace) -> int:
    evidence_csv = _resolve_evidence_csv()
    if not evidence_csv.is_file():
        print(f"ERROR: evidence matrix not found at {evidence_csv}")
        return 1
    rows = load_csv(evidence_csv)
    ready = sorted({
        r["pathway_id"] for r in rows
        if r.get("entity_type") == "true_pathway"
        and pathway_is_provisionally_ready(rows, r["pathway_id"])
    })
    reviews = audit_readiness(rows, provisionally_ready=ready, pathways_to_audit=ready or None)
    summary = summarize_readiness(reviews)
    review_path = RUNS_DIR / "readiness_review.csv"
    packet_path = RUNS_DIR / "human_review_packet.md"
    summary_path = RUNS_DIR / "readiness_summary.json"
    write_readiness_csv(reviews, review_path)
    packet_path.write_text(generate_human_review_packet(reviews, rows), encoding="utf-8")
    summary_path.write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
    print(f"Provisionally ready: {len(ready)}")
    for pid in ready:
        print(f"  - {pid}")
    print(f"Review: {review_path}")
    print(f"Packet: {packet_path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    evidence_csv = _resolve_evidence_csv()
    if not evidence_csv.is_file():
        print(f"ERROR: evidence matrix not found at {evidence_csv}")
        return 1
    rows = load_csv(evidence_csv)
    snap = coverage_snapshot(rows)
    card = verify_matrix_cardinality(rows)
    print(f"Evidence matrix: {evidence_csv}")
    print(f"Cardinality ok: {card.get('ok')} ({card.get('true_pathway_cells')} cells)")
    print(f"Classifications: {snap.get('classification_counts')}")
    print(f"Classification-ready pathways: {len(snap.get('provisionally_complete_pathways') or [])}")
    ready = [
        pid for pid in sorted({r['pathway_id'] for r in rows if r.get('entity_type') == 'true_pathway'})
        if pathway_is_provisionally_ready(rows, pid)
    ]
    print(f"Quality-ready pathways ({len(ready)}): {ready}")
    if getattr(args, "with_db", False) and DATABASE_URL:
        print(f"Corpus counts: {get_corpus_counts(DATABASE_URL)}")
    cov = RUNS_DIR / COVERAGE_MATRIX_NAME
    if cov.is_file():
        print(f"Coverage matrix: {cov}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    evidence_csv = _resolve_evidence_csv()
    rows = load_csv(evidence_csv) if evidence_csv.is_file() else []
    row = next(
        (
            r for r in rows
            if r.get("pathway_id") == args.pathway_id
            and r.get("requirement") == args.requirement
        ),
        None,
    )
    if row is None and not args.allow_missing:
        print(
            f"ERROR: cell not found for {args.pathway_id}/{args.requirement} in {evidence_csv}. "
            "Pass --allow-missing to record anyway."
        )
        return 1
    if args.show and row:
        print(format_cell_for_review(row))
        print("")
    decision = record_decision(
        runs_dir=RUNS_DIR,
        pathway_id=args.pathway_id,
        requirement=args.requirement,
        decision=args.decision,
        reviewer_id=args.reviewer,
        evidence_row=row,
        explanation=args.explanation or "",
    )
    print(
        f"Recorded {decision.decision} by {decision.reviewer_id} "
        f"for {decision.pathway_id}/{decision.requirement}"
    )
    print(f"Log: {reviews_path(RUNS_DIR)}")
    return 0


def cmd_confirm_pathway(args: argparse.Namespace) -> int:
    evidence_csv = _resolve_evidence_csv()
    rows = load_csv(evidence_csv) if evidence_csv.is_file() else []
    result = confirm_pathway(
        runs_dir=RUNS_DIR,
        pathway_id=args.pathway_id,
        reviewer_id=args.reviewer,
        evidence_rows=rows,
        critical_requirements=list(CRITICAL_REQUIREMENTS),
        explanation=args.explanation or "",
        allow_incomplete=bool(getattr(args, "allow_incomplete", False)),
    )
    print(result.get("message"))
    if result.get("blocking_issues"):
        for issue in result["blocking_issues"]:
            print(f"  - {issue}")
    print(f"human_verified={result.get('human_verified')}")
    return 0 if result.get("accepted") else 1


def cmd_discovery(args: argparse.Namespace) -> int:
    rows = build_discovery_candidates()
    out = RUNS_DIR / "source_discovery_candidates.csv"
    write_discovery_csv(rows, out)
    print(f"Wrote {len(rows)} candidates to {out}")
    print("No URLs were fetched. Owner approval required before ingestion.")
    for r in rows:
        print(f"  [{r['jurisdiction']}] {r['pathway']}: {r.get('url') or '(URL research pending)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corpus reliability operations")
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="Create backup")
    backup.add_argument("--skip-extracted", action="store_true")
    backup.add_argument("--skip-compared", action="store_true")
    backup.add_argument("--include-runs", action="store_true")

    verify = sub.add_parser("verify-backup", help="Verify backup")
    verify.add_argument("--backup-dir", type=str)

    restore = sub.add_parser("restore-rehearsal", help="Test restore")
    restore.add_argument("--backup-dir", type=str)

    sub.add_parser("consistency-audit", help="Audit evidence matrix consistency")
    assess = sub.add_parser("assess", help="Batch assess UNEVALUATED cells")
    assess.add_argument("--max-cells", type=int, default=50)

    sub.add_parser("readiness-audit", help="Audit provisionally ready pathways")
    status = sub.add_parser("status", help="Show current corpus coverage/readiness status")
    status.add_argument("--with-db", action="store_true", help="Also query live DB corpus counts")
    sub.add_parser("discovery", help="Write discovery candidates (no fetch)")

    review = sub.add_parser("review", help="Record human review for one cell")
    review.add_argument("--pathway-id", required=True)
    review.add_argument("--requirement", required=True)
    review.add_argument("--decision", required=True, choices=["confirm", "reject", "defer"])
    review.add_argument("--reviewer", required=True)
    review.add_argument("--explanation", default="")
    review.add_argument("--show", action="store_true")
    review.add_argument("--allow-missing", action="store_true")

    confirm = sub.add_parser("confirm-pathway", help="Pathway-level human verification")
    confirm.add_argument("--pathway-id", required=True)
    confirm.add_argument("--reviewer", required=True)
    confirm.add_argument("--explanation", default="")
    confirm.add_argument("--allow-incomplete", action="store_true")

    args = parser.parse_args(argv)
    dispatch = {
        "backup": cmd_backup,
        "verify-backup": cmd_verify_backup,
        "restore-rehearsal": cmd_restore_rehearsal,
        "consistency-audit": cmd_consistency_audit,
        "assess": cmd_assess,
        "readiness-audit": cmd_readiness_audit,
        "status": cmd_status,
        "discovery": cmd_discovery,
        "review": cmd_review,
        "confirm-pathway": cmd_confirm_pathway,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
