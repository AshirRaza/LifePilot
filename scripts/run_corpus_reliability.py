"""CLI entry point for corpus reliability operations.

Operations:
- backup: Create backup of database and data files
- verify-backup: Verify backup integrity
- restore-rehearsal: Test restore to isolated DB and directory
- consistency-audit: Audit evidence matrix for consistency issues
- assess: Batch assess UNEVALUATED cells
- readiness-audit: Audit provisionally ready pathways
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from lifepilot.coverage.corpus_reliability.backup import (
    BackupError,
    RestoreError,
    cleanup_restore_database,
    create_backup,
    restore_data_files,
    restore_database,
    verify_backup,
    get_corpus_counts,
    sha256_file,
)
from lifepilot.coverage.corpus_reliability.consistency import (
    audit_evidence_consistency,
    generate_repair_proposals,
    load_evidence_matrix,
    write_audit_csv,
    write_repairs_csv,
)
from lifepilot.coverage.corpus_reliability.assessment import (
    batch_assess,
    write_assessments_csv,
    write_changes_csv,
)
from lifepilot.coverage.corpus_reliability.readiness import (
    audit_readiness,
    generate_human_review_packet,
    write_readiness_csv,
    AUDIT_PATHWAYS,
)
from lifepilot.db.session import get_session_factory

RUNS_DIR = ROOT / "data" / "runs"
BACKUPS_DIR = ROOT / "backups"
DATA_DIR = ROOT / "data"


def cmd_backup(args: argparse.Namespace) -> int:
    """Create a backup."""
    db_url = os.environ.get("LIFEPILOT_APP_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        return 1
    
    print(f"Creating backup...")
    print(f"  Database URL: {db_url.split('@')[1] if '@' in db_url else db_url}")  # Hide password
    print(f"  Backups dir: {BACKUPS_DIR}")
    
    try:
        result = create_backup(
            db_url=db_url,
            data_dir=DATA_DIR,
            backups_dir=BACKUPS_DIR,
            include_extracted=not args.skip_extracted,
            include_compared=not args.skip_compared,
            include_runs=args.include_runs,
        )
        
        manifest = result["manifest"]
        print(f"\nBackup created: {result['backup_dir']}")
        print(f"  Manifest: {result['manifest_path']}")
        print(f"  Backup ID: {manifest['backup_id']}")
        print(f"  Database dump: {manifest['database']['dump_file']} ({manifest['database']['dump_size']:,} bytes)")
        print(f"  Data archive: {manifest['data_archive']['file']} ({manifest['data_archive']['size']:,} bytes)")
        print(f"  Files archived: {manifest['data_archive']['file_count']}")
        print(f"\nCorpus counts:")
        for k, v in manifest["corpus_counts"].items():
            print(f"    {k}: {v}")
        
        # Write manifest path for subsequent commands
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        manifest_ref = RUNS_DIR / "phase_6i_backup_manifest.json"
        manifest_ref.write_text(json.dumps({
            "backup_dir": result["backup_dir"],
            "manifest_path": result["manifest_path"],
            "created_at": manifest["created_at"],
            "backup_id": manifest["backup_id"],
            "corpus_counts": manifest["corpus_counts"],
            "database_dump_sha256": manifest["database"]["dump_sha256"],
            "data_archive_sha256": manifest["data_archive"]["sha256"],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nManifest reference: {manifest_ref}")
        
        return 0
    except BackupError as e:
        print(f"ERROR: {e}")
        return 1


def cmd_verify_backup(args: argparse.Namespace) -> int:
    """Verify a backup."""
    backup_dir = Path(args.backup_dir) if args.backup_dir else None
    
    if not backup_dir:
        # Try to find from manifest reference
        manifest_ref = RUNS_DIR / "phase_6i_backup_manifest.json"
        if manifest_ref.is_file():
            ref = json.loads(manifest_ref.read_text(encoding="utf-8"))
            backup_dir = Path(ref["backup_dir"])
    
    if not backup_dir or not backup_dir.is_dir():
        print("ERROR: No backup directory specified and no recent backup found")
        return 1
    
    print(f"Verifying backup: {backup_dir}")
    
    try:
        result = verify_backup(backup_dir)
        print(f"\nBackup ID: {result['backup_id']}")
        print(f"Created: {result['created_at']}")
        print(f"Status: {result['status']}")
        
        if result["issues"]:
            print("\nIssues found:")
            for issue in result["issues"]:
                print(f"  - {issue}")
            return 1
        
        print("\nCorpus counts at backup time:")
        for k, v in result["corpus_counts"].items():
            print(f"    {k}: {v}")
        
        print("\nBackup verified OK")
        return 0
    except BackupError as e:
        print(f"ERROR: {e}")
        return 1


def cmd_restore_rehearsal(args: argparse.Namespace) -> int:
    """Test restore to isolated DB and directory."""
    db_url = os.environ.get("LIFEPILOT_APP_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        return 1
    
    backup_dir = Path(args.backup_dir) if args.backup_dir else None
    
    if not backup_dir:
        manifest_ref = RUNS_DIR / "phase_6i_backup_manifest.json"
        if manifest_ref.is_file():
            ref = json.loads(manifest_ref.read_text(encoding="utf-8"))
            backup_dir = Path(ref["backup_dir"])
    
    if not backup_dir or not backup_dir.is_dir():
        print("ERROR: No backup directory specified")
        return 1
    
    target_db = args.target_db or "lifepilot_restore_test"
    target_dir = Path(args.target_dir) if args.target_dir else BACKUPS_DIR / "restore_rehearsal"
    
    print(f"Restore rehearsal:")
    print(f"  Backup: {backup_dir}")
    print(f"  Target DB: {target_db}")
    print(f"  Target dir: {target_dir}")
    
    verification: dict = {
        "backup_dir": str(backup_dir),
        "target_db": target_db,
        "target_dir": str(target_dir),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    
    try:
        # 1. Restore database
        print("\n1. Restoring database...")
        db_result = restore_database(
            backup_dir=backup_dir,
            target_db_name=target_db,
            db_url=db_url,
        )
        verification["db_restore"] = db_result
        print(f"   Status: {db_result['status']}")
        print(f"   Counts match: {db_result['counts_match']}")
        
        if not db_result["counts_match"]:
            print(f"   Mismatches: {db_result['mismatches']}")
        
        # 2. Restore data files
        print("\n2. Restoring data files...")
        files_result = restore_data_files(
            backup_dir=backup_dir,
            target_dir=target_dir,
        )
        verification["files_restore"] = files_result
        print(f"   Status: {files_result['status']}")
        print(f"   Files verified: {files_result['files_verified']}/{files_result['files_expected']}")
        
        if files_result["files_failed"]:
            print(f"   Failed: {len(files_result['files_failed'])}")
            for f in files_result["files_failed"][:5]:
                print(f"     - {f['path']}: {f['error']}")
        
        # 3. Cross-verify: check version storage paths resolve
        print("\n3. Cross-verifying storage paths...")
        from sqlalchemy import create_engine, text as sql_text
        from sqlalchemy.engine import make_url
        
        restore_url = make_url(db_url).set(database=target_db).render_as_string(hide_password=False)
        engine = create_engine(restore_url)
        
        with engine.connect() as conn:
            paths = conn.execute(
                sql_text("SELECT id, storage_path FROM document_versions LIMIT 100")
            ).mappings().all()
        
        verified_paths = 0
        missing_paths = []
        for row in paths:
            path = target_dir / "data" / row["storage_path"]
            if path.is_file():
                verified_paths += 1
            else:
                missing_paths.append(row["storage_path"])
        
        verification["path_verification"] = {
            "checked": len(paths),
            "verified": verified_paths,
            "missing": missing_paths[:10],
        }
        print(f"   Paths checked: {len(paths)}")
        print(f"   Paths verified: {verified_paths}")
        if missing_paths:
            print(f"   Missing (first 5): {missing_paths[:5]}")
        
        engine.dispose()
        
        # 4. Cleanup restore DB if requested
        if args.cleanup:
            print("\n4. Cleaning up restore database...")
            cleanup = cleanup_restore_database(
                db_name=target_db,
                db_url=db_url,
            )
            verification["cleanup"] = cleanup
            print(f"   Dropped: {cleanup['dropped']}")
        
        verification["completed_at"] = datetime.now(timezone.utc).isoformat()
        verification["overall_status"] = (
            "ok" if db_result["status"] == "ok" and files_result["status"] == "ok"
            else "failed"
        )
        
        # Write verification report
        report_path = RUNS_DIR / "phase_6i_restore_verification.json"
        report_path.write_text(json.dumps(verification, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nVerification report: {report_path}")
        print(f"Overall status: {verification['overall_status']}")
        
        return 0 if verification["overall_status"] == "ok" else 1
    
    except RestoreError as e:
        print(f"ERROR: {e}")
        verification["error"] = str(e)
        verification["overall_status"] = "blocked"
        
        report_path = RUNS_DIR / "phase_6i_restore_verification.json"
        report_path.write_text(json.dumps(verification, indent=2, ensure_ascii=False), encoding="utf-8")
        
        return 1


def cmd_consistency_audit(args: argparse.Namespace) -> int:
    """Audit evidence matrix for consistency issues."""
    # Load evidence matrix
    evidence_csv = RUNS_DIR / "phase_6h_evidence_changes.csv"
    if not evidence_csv.is_file():
        print(f"ERROR: Evidence matrix not found: {evidence_csv}")
        return 1
    
    print(f"Loading evidence matrix: {evidence_csv}")
    evidence_rows = load_evidence_matrix(evidence_csv)
    print(f"  Rows loaded: {len(evidence_rows)}")
    
    print("\nAuditing consistency...")
    factory = get_session_factory()
    with factory() as session:
        issues = audit_evidence_consistency(session, evidence_rows)
    
    print(f"  Issues found: {len(issues)}")
    
    # Group by severity
    by_severity: dict[str, int] = {}
    for i in issues:
        by_severity[i["severity"]] = by_severity.get(i["severity"], 0) + 1
    for sev, count in sorted(by_severity.items()):
        print(f"    {sev}: {count}")
    
    # Generate repair proposals
    repairs = generate_repair_proposals(issues)
    print(f"  Auto-repairable: {len(repairs)}")
    
    # Write outputs
    audit_path = RUNS_DIR / "phase_6i_consistency_audit.csv"
    repairs_path = RUNS_DIR / "phase_6i_consistency_repairs.csv"
    
    write_audit_csv(issues, audit_path)
    write_repairs_csv(repairs, repairs_path)
    
    print(f"\nOutputs:")
    print(f"  Audit: {audit_path}")
    print(f"  Repairs: {repairs_path}")
    
    return 0


def cmd_assess(args: argparse.Namespace) -> int:
    """Batch assess UNEVALUATED cells."""
    evidence_csv = RUNS_DIR / "phase_6h_evidence_changes.csv"
    if not evidence_csv.is_file():
        print(f"ERROR: Evidence matrix not found: {evidence_csv}")
        return 1
    
    print(f"Loading evidence matrix: {evidence_csv}")
    evidence_rows = load_evidence_matrix(evidence_csv)
    
    def get_cls(r: dict) -> str:
        return r.get("classification") or r.get("classification_6h", "UNEVALUATED")
    
    unevaluated = [r for r in evidence_rows if get_cls(r) == "UNEVALUATED"]
    print(f"  UNEVALUATED cells: {len(unevaluated)}")
    
    max_assessments = args.max_assessments or 100
    print(f"\nBatch assessing (max {max_assessments})...")
    
    factory = get_session_factory()
    with factory() as session:
        assessments, changes = batch_assess(
            session, evidence_rows, DATA_DIR,
            max_assessments=max_assessments,
        )
    
    print(f"  Assessments generated: {len(assessments)}")
    print(f"  Evidence changes: {len(changes)}")
    
    # Write outputs
    assessments_path = RUNS_DIR / "phase_6i_evidence_assessments.csv"
    changes_path = RUNS_DIR / "phase_6i_evidence_changes.csv"
    
    write_assessments_csv(assessments, assessments_path)
    write_changes_csv(changes, changes_path)
    
    print(f"\nOutputs:")
    print(f"  Assessments: {assessments_path}")
    print(f"  Changes: {changes_path}")
    
    return 0


def cmd_readiness_audit(args: argparse.Namespace) -> int:
    """Audit provisionally ready pathways."""
    evidence_csv = RUNS_DIR / "phase_6h_evidence_changes.csv"
    readiness_json = RUNS_DIR / "phase_6h_readiness.json"
    
    if not evidence_csv.is_file():
        print(f"ERROR: Evidence matrix not found: {evidence_csv}")
        return 1
    
    print(f"Loading evidence matrix: {evidence_csv}")
    evidence_rows = load_evidence_matrix(evidence_csv)
    
    # Load provisionally ready list from 6H
    provisionally_ready = []
    if readiness_json.is_file():
        readiness = json.loads(readiness_json.read_text(encoding="utf-8"))
        provisionally_ready = readiness.get("provisionally_ready_pathways", [])
    print(f"  Provisionally ready (from 6H): {provisionally_ready}")
    
    print(f"\nAuditing readiness for: {AUDIT_PATHWAYS}")
    reviews = audit_readiness(evidence_rows, provisionally_ready)
    
    # Summary
    by_decision: dict[str, int] = {}
    for r in reviews:
        by_decision[r.decision] = by_decision.get(r.decision, 0) + 1
    
    print(f"\nDecisions:")
    for dec, count in sorted(by_decision.items()):
        print(f"  {dec}: {count}")
    
    # Write outputs
    review_path = RUNS_DIR / "phase_6i_readiness_review.csv"
    packet_path = RUNS_DIR / "phase_6i_human_review_packet.md"
    
    write_readiness_csv(reviews, review_path)
    packet_path.write_text(
        generate_human_review_packet(reviews, evidence_rows),
        encoding="utf-8"
    )
    
    print(f"\nOutputs:")
    print(f"  Review: {review_path}")
    print(f"  Human review packet: {packet_path}")
    
    # Verify human_verified is always False
    for r in reviews:
        assert r.human_verified is False, f"human_verified must be False: {r.pathway_id}"
    print("\nInvariant verified: human_verified=False for all reviews")
    
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Corpus reliability operations"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # backup
    backup_parser = subparsers.add_parser("backup", help="Create backup")
    backup_parser.add_argument("--skip-extracted", action="store_true")
    backup_parser.add_argument("--skip-compared", action="store_true")
    backup_parser.add_argument("--include-runs", action="store_true")
    
    # verify-backup
    verify_parser = subparsers.add_parser("verify-backup", help="Verify backup")
    verify_parser.add_argument("--backup-dir", type=str)
    
    # restore-rehearsal
    restore_parser = subparsers.add_parser("restore-rehearsal", help="Test restore")
    restore_parser.add_argument("--backup-dir", type=str)
    restore_parser.add_argument("--target-db", type=str, default="lifepilot_restore_test")
    restore_parser.add_argument("--target-dir", type=str)
    restore_parser.add_argument("--cleanup", action="store_true", help="Drop restore DB after")
    
    # consistency-audit
    subparsers.add_parser("consistency-audit", help="Audit evidence consistency")
    
    # assess
    assess_parser = subparsers.add_parser("assess", help="Batch assess UNEVALUATED")
    assess_parser.add_argument("--max-assessments", type=int, default=100)
    
    # readiness-audit
    subparsers.add_parser("readiness-audit", help="Audit readiness")
    
    args = parser.parse_args(argv)
    
    if args.command == "backup":
        return cmd_backup(args)
    elif args.command == "verify-backup":
        return cmd_verify_backup(args)
    elif args.command == "restore-rehearsal":
        return cmd_restore_rehearsal(args)
    elif args.command == "consistency-audit":
        return cmd_consistency_audit(args)
    elif args.command == "assess":
        return cmd_assess(args)
    elif args.command == "readiness-audit":
        return cmd_readiness_audit(args)
    
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
