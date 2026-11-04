"""Backup and restore functionality for LifePilot corpus.

Implements practical backup+restore:
- pg_dump of live app DB (schema+data) via PostgreSQL tools inside Docker.
- Archive host `data/raw/` (and extracted/compared/runs configs without secrets).
- Manifest JSON: paths, sizes, sha256, timestamps, DB snapshot id, corpus counts.
- CLI: create / verify / restore-to-isolated-dir.
- Restore into SEPARATE disposable DB name ending `_restore_test` or similar
  AND separate directory — NEVER overwrite live lifepilot DB or live data/.

Recovery documentation:
- If DB volume is lost: restore from pg_dump to a fresh database.
- If data/ is lost: restore from the archived tarball to data/.
- Both lost: restore DB first, then data/ files, verify manifest hashes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_BACKUPS_DIR = REPO_ROOT / "backups"
DEFAULT_DATA_DIR = REPO_ROOT / "data"

# Positive identification: name must end with _restore_test or _backup_test etc.
_RESTORE_DB_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*_(restore|backup)_test$")
_FORBIDDEN_DB_NAMES = frozenset({
    "postgres", "template0", "template1", "lifepilot", "lifepilot_test"
})


class BackupError(RuntimeError):
    """Raised when backup creation or verification fails."""


class RestoreError(RuntimeError):
    """Raised when restore operation fails or is unsafe."""


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def file_info(path: Path, base: Path) -> dict[str, Any]:
    """Get file info relative to base path."""
    stat = path.stat()
    return {
        "path": str(path.relative_to(base)),
        "size": stat.st_size,
        "sha256": sha256_file(path),
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def get_corpus_counts(db_url: str) -> dict[str, int]:
    """Get corpus counts from the database."""
    engine = create_engine(db_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT
                        (SELECT count(*) FROM sources WHERE review_status='approved') AS approved_sources,
                        (SELECT count(*) FROM documents) AS documents,
                        (SELECT count(*) FROM document_versions) AS document_versions,
                        (SELECT count(*) FROM document_extractions WHERE status='success') AS success_extractions,
                        (SELECT count(*) FROM sources) AS total_sources,
                        (SELECT count(*) FROM document_extractions) AS total_extractions,
                        (SELECT count(*) FROM discovery_frontier) AS frontier_rows
                """)
            ).mappings().one()
            return dict(row)
    finally:
        engine.dispose()


def assert_safe_restore_database(db_name: str) -> None:
    """Fail closed unless the DB name is positively identified as a restore-safe DB."""
    if db_name in _FORBIDDEN_DB_NAMES:
        raise RestoreError(
            f"Refusing to restore to database '{db_name}': protected name."
        )
    if not _RESTORE_DB_NAME_RE.match(db_name):
        raise RestoreError(
            f"Refusing to restore to database '{db_name}': name must end with "
            f"'_restore_test' or '_backup_test' (e.g. 'lifepilot_restore_test')."
        )


def create_backup(
    *,
    db_url: str,
    data_dir: Path | None = None,
    backups_dir: Path | None = None,
    backup_id: str | None = None,
    docker_compose_cmd: list[str] | None = None,
    include_extracted: bool = True,
    include_compared: bool = True,
    include_runs: bool = False,  # runs/ often has phase artifacts we don't want to archive
) -> dict[str, Any]:
    """Create a backup of the database and data files.
    
    Args:
        db_url: Database URL (e.g. from DATABASE_URL env).
        data_dir: Path to data/ directory (default: REPO_ROOT/data).
        backups_dir: Where to store backups (default: REPO_ROOT/backups).
        backup_id: Optional ID for the backup (default: timestamp).
        docker_compose_cmd: Command prefix for docker compose (default: ["docker", "compose"]).
        include_extracted: Include data/extracted/ in archive.
        include_compared: Include data/compared/ in archive.
        include_runs: Include data/runs/ in archive.
    
    Returns:
        Manifest dict with paths, sizes, sha256, timestamps, corpus counts.
    """
    data_dir = data_dir or DEFAULT_DATA_DIR
    backups_dir = backups_dir or DEFAULT_BACKUPS_DIR
    docker_compose_cmd = docker_compose_cmd or ["docker", "compose"]
    
    if not data_dir.is_dir():
        raise BackupError(f"Data directory not found: {data_dir}")
    
    # Generate backup ID
    now = datetime.now(timezone.utc)
    backup_id = backup_id or now.strftime("%Y%m%d_%H%M%S")
    backup_dir = backups_dir / f"backup_{backup_id}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    # Parse DB connection info
    url = make_url(db_url)
    db_name = url.database
    db_user = url.username
    
    if not db_name or not db_user:
        raise BackupError("Database URL must include database name and user.")
    
    # 1. pg_dump via docker compose exec
    dump_file = backup_dir / f"{db_name}.sql"
    pg_dump_cmd = docker_compose_cmd + [
        "exec", "-T", "postgres",
        "pg_dump", "-U", db_user, "-d", db_name,
        "--clean", "--if-exists", "--create",
    ]
    
    try:
        with dump_file.open("w", encoding="utf-8") as f:
            result = subprocess.run(
                pg_dump_cmd,
                stdout=f,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(REPO_ROOT),
                timeout=300,
            )
        if result.returncode != 0:
            raise BackupError(f"pg_dump failed: {result.stderr}")
    except subprocess.TimeoutExpired:
        raise BackupError("pg_dump timed out after 5 minutes")
    
    # 2. Get corpus counts before archiving
    counts = get_corpus_counts(db_url)
    
    # 3. Archive data directories
    data_archive = backup_dir / "data.tar.gz"
    data_files: list[dict[str, Any]] = []
    
    with tarfile.open(data_archive, "w:gz") as tar:
        # Always include raw/
        raw_dir = data_dir / "raw"
        if raw_dir.is_dir():
            for f in raw_dir.rglob("*"):
                if f.is_file():
                    arcname = f"data/{f.relative_to(data_dir)}"
                    tar.add(f, arcname=arcname)
                    data_files.append(file_info(f, data_dir))
        
        # Optionally include extracted/
        if include_extracted:
            extracted_dir = data_dir / "extracted"
            if extracted_dir.is_dir():
                for f in extracted_dir.rglob("*"):
                    if f.is_file():
                        arcname = f"data/{f.relative_to(data_dir)}"
                        tar.add(f, arcname=arcname)
                        data_files.append(file_info(f, data_dir))
        
        # Optionally include compared/
        if include_compared:
            compared_dir = data_dir / "compared"
            if compared_dir.is_dir():
                for f in compared_dir.rglob("*"):
                    if f.is_file():
                        arcname = f"data/{f.relative_to(data_dir)}"
                        tar.add(f, arcname=arcname)
                        data_files.append(file_info(f, data_dir))
        
        # Optionally include runs/ (usually excluded)
        if include_runs:
            runs_dir = data_dir / "runs"
            if runs_dir.is_dir():
                for f in runs_dir.rglob("*"):
                    if f.is_file() and not f.name.startswith("."):
                        arcname = f"data/{f.relative_to(data_dir)}"
                        tar.add(f, arcname=arcname)
                        data_files.append(file_info(f, data_dir))
    
    # 4. Build manifest
    manifest = {
        "backup_id": backup_id,
        "created_at": now.isoformat(),
        "database": {
            "name": db_name,
            "user": db_user,
            "dump_file": dump_file.name,
            "dump_sha256": sha256_file(dump_file),
            "dump_size": dump_file.stat().st_size,
        },
        "corpus_counts": counts,
        "data_archive": {
            "file": data_archive.name,
            "sha256": sha256_file(data_archive),
            "size": data_archive.stat().st_size,
            "included_dirs": {
                "raw": True,
                "extracted": include_extracted,
                "compared": include_compared,
                "runs": include_runs,
            },
            "file_count": len(data_files),
        },
        "files": data_files,
        "recovery_instructions": {
            "db_volume_lost": (
                "1. Create restore DB: docker compose exec postgres psql -U lifepilot -d postgres "
                f"-c \"CREATE DATABASE lifepilot_restore_test\"\n"
                f"2. Restore: docker compose exec -T postgres psql -U lifepilot -d lifepilot_restore_test "
                f"< {dump_file.name}\n"
                "3. Verify counts match manifest.corpus_counts"
            ),
            "data_dir_lost": (
                f"1. Extract: tar -xzf {data_archive.name} -C /path/to/restore/\n"
                "2. Verify SHA-256 hashes match manifest.files entries\n"
                "3. Update DATABASE_URL storage_path references if DB was also restored"
            ),
            "both_lost": (
                "1. Restore DB first (see db_volume_lost)\n"
                "2. Restore data/ (see data_dir_lost)\n"
                "3. Verify document_versions.storage_path references match restored files"
            ),
        },
    }
    
    manifest_path = backup_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    
    return {
        "backup_dir": str(backup_dir),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
    }


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    """Verify a backup's integrity against its manifest.
    
    Returns:
        Verification result with status and any mismatches.
    """
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BackupError(f"Manifest not found: {manifest_path}")
    
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    issues: list[str] = []
    
    # Check dump file
    dump_file = backup_dir / manifest["database"]["dump_file"]
    if not dump_file.is_file():
        issues.append(f"Dump file missing: {dump_file.name}")
    else:
        actual_sha = sha256_file(dump_file)
        if actual_sha != manifest["database"]["dump_sha256"]:
            issues.append(f"Dump file hash mismatch: expected {manifest['database']['dump_sha256'][:16]}..., got {actual_sha[:16]}...")
    
    # Check data archive
    data_archive = backup_dir / manifest["data_archive"]["file"]
    if not data_archive.is_file():
        issues.append(f"Data archive missing: {data_archive.name}")
    else:
        actual_sha = sha256_file(data_archive)
        if actual_sha != manifest["data_archive"]["sha256"]:
            issues.append(f"Data archive hash mismatch: expected {manifest['data_archive']['sha256'][:16]}..., got {actual_sha[:16]}...")
    
    return {
        "backup_id": manifest["backup_id"],
        "created_at": manifest["created_at"],
        "status": "ok" if not issues else "failed",
        "issues": issues,
        "corpus_counts": manifest["corpus_counts"],
    }


def restore_database(
    *,
    backup_dir: Path,
    target_db_name: str,
    db_url: str,
    docker_compose_cmd: list[str] | None = None,
) -> dict[str, Any]:
    """Restore database from a backup to an isolated restore-test database.
    
    SAFETY: Refuses to restore to 'lifepilot', 'lifepilot_test', 'postgres', etc.
    Target DB name must end with '_restore_test' or '_backup_test'.
    
    Args:
        backup_dir: Path to the backup directory containing manifest.json.
        target_db_name: Name of the target database (must be restore-safe).
        db_url: Original database URL (used for connection params).
        docker_compose_cmd: Docker compose command prefix.
    
    Returns:
        Restore result with counts verification.
    """
    # SAFETY CHECK: refuse to restore to live DB
    assert_safe_restore_database(target_db_name)
    
    docker_compose_cmd = docker_compose_cmd or ["docker", "compose"]
    
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RestoreError(f"Manifest not found: {manifest_path}")
    
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dump_file = backup_dir / manifest["database"]["dump_file"]
    
    if not dump_file.is_file():
        raise RestoreError(f"Dump file not found: {dump_file}")
    
    # Verify dump file integrity
    actual_sha = sha256_file(dump_file)
    if actual_sha != manifest["database"]["dump_sha256"]:
        raise RestoreError("Dump file SHA-256 mismatch - backup may be corrupted")
    
    url = make_url(db_url)
    db_user = url.username
    
    # 1. Create the target database (drop if exists)
    # DROP DATABASE cannot run inside a transaction, so we run them as separate commands
    drop_cmd = docker_compose_cmd + [
        "exec", "-T", "postgres",
        "psql", "-U", db_user, "-d", "postgres",
        "-c", f'DROP DATABASE IF EXISTS "{target_db_name}";',
    ]
    result = subprocess.run(
        drop_cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    # DROP might fail if DB doesn't exist or has active connections - that's OK
    
    create_cmd = docker_compose_cmd + [
        "exec", "-T", "postgres",
        "psql", "-U", db_user, "-d", "postgres",
        "-c", f'CREATE DATABASE "{target_db_name}";',
    ]
    result = subprocess.run(
        create_cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0 and "already exists" not in (result.stderr or ""):
        raise RestoreError(f"Failed to create target database: {result.stderr}")
    
    # 2. Restore from dump
    # We need to modify the dump to target the new database
    # pg_dump with --create generates commands for the original DB name
    # We'll restore just the schema/data without the CREATE DATABASE part
    
    # Read the dump and filter out CREATE DATABASE / DROP DATABASE / connect commands
    dump_content = dump_file.read_text(encoding="utf-8")
    filtered_lines = []
    skip_patterns = [
        "CREATE DATABASE",
        "DROP DATABASE",
        f"\\connect {manifest['database']['name']}",
    ]
    for line in dump_content.splitlines():
        if not any(p in line for p in skip_patterns):
            filtered_lines.append(line)
    filtered_dump = "\n".join(filtered_lines)
    
    # Write filtered dump to a temp file to avoid encoding issues with stdin
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".sql", delete=False) as tf:
        tf.write(filtered_dump)
        temp_sql = tf.name
    
    try:
        # Copy the temp file into the container and restore from it
        # Use docker cp to copy the file into the container
        container_path = "/tmp/restore_dump.sql"
        
        # Get the container ID
        get_container_cmd = docker_compose_cmd + ["ps", "-q", "postgres"]
        container_result = subprocess.run(
            get_container_cmd,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        container_id = container_result.stdout.strip()
        
        if not container_id:
            raise RestoreError("Could not find postgres container")
        
        # Copy file to container
        copy_cmd = ["docker", "cp", temp_sql, f"{container_id}:{container_path}"]
        subprocess.run(copy_cmd, check=True, cwd=str(REPO_ROOT))
        
        # Restore from the file inside the container
        restore_cmd = docker_compose_cmd + [
            "exec", "-T", "postgres",
            "psql", "-U", db_user, "-d", target_db_name, "-f", container_path,
        ]
        
        result = subprocess.run(
            restore_cmd,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        
        # Cleanup the temp file in container
        cleanup_cmd = docker_compose_cmd + [
            "exec", "-T", "postgres",
            "rm", "-f", container_path,
        ]
        subprocess.run(cleanup_cmd, cwd=str(REPO_ROOT))
        
    finally:
        # Remove local temp file
        Path(temp_sql).unlink(missing_ok=True)
    
    # Note: psql may return warnings which aren't failures
    if result.returncode != 0 and "ERROR" in (result.stderr or ""):
        raise RestoreError(f"Restore errors: {result.stderr}")
    
    # 3. Verify counts
    restore_url = url.set(database=target_db_name).render_as_string(hide_password=False)
    restored_counts = get_corpus_counts(restore_url)
    
    expected = manifest["corpus_counts"]
    mismatches = {}
    for key in ("approved_sources", "documents", "document_versions", "success_extractions"):
        if int(restored_counts.get(key, 0)) != int(expected.get(key, 0)):
            mismatches[key] = {"expected": expected[key], "actual": restored_counts[key]}
    
    return {
        "target_db": target_db_name,
        "backup_id": manifest["backup_id"],
        "expected_counts": expected,
        "restored_counts": restored_counts,
        "counts_match": not mismatches,
        "mismatches": mismatches,
        "status": "ok" if not mismatches else "counts_mismatch",
    }


def restore_data_files(
    *,
    backup_dir: Path,
    target_dir: Path,
) -> dict[str, Any]:
    """Restore data files from a backup to an isolated directory.
    
    SAFETY: Refuses to restore to the live data/ directory.
    
    Args:
        backup_dir: Path to the backup directory containing manifest.json.
        target_dir: Path to extract files to (must not be the live data/).
    
    Returns:
        Restore result with file verification.
    """
    # SAFETY CHECK: refuse to restore to live data/
    target_resolved = target_dir.resolve()
    live_data = DEFAULT_DATA_DIR.resolve()
    if target_resolved == live_data or str(target_resolved).startswith(str(live_data) + os.sep):
        raise RestoreError(
            f"Refusing to restore to live data directory: {target_dir}. "
            f"Use an isolated directory like backups/restore_rehearsal/"
        )
    
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RestoreError(f"Manifest not found: {manifest_path}")
    
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    data_archive = backup_dir / manifest["data_archive"]["file"]
    
    if not data_archive.is_file():
        raise RestoreError(f"Data archive not found: {data_archive}")
    
    # Verify archive integrity
    actual_sha = sha256_file(data_archive)
    if actual_sha != manifest["data_archive"]["sha256"]:
        raise RestoreError("Data archive SHA-256 mismatch - backup may be corrupted")
    
    # Create target directory
    target_dir.mkdir(parents=True, exist_ok=True)
    
    # Extract archive
    with tarfile.open(data_archive, "r:gz") as tar:
        tar.extractall(target_dir)
    
    # Verify individual files against manifest
    verified = 0
    failed = []
    for file_entry in manifest.get("files", []):
        rel_path = file_entry["path"]
        full_path = target_dir / "data" / rel_path
        if not full_path.is_file():
            failed.append({"path": rel_path, "error": "missing"})
            continue
        actual_sha = sha256_file(full_path)
        if actual_sha != file_entry["sha256"]:
            failed.append({"path": rel_path, "error": "hash_mismatch"})
            continue
        verified += 1
    
    return {
        "backup_id": manifest["backup_id"],
        "target_dir": str(target_dir),
        "files_expected": len(manifest.get("files", [])),
        "files_verified": verified,
        "files_failed": failed,
        "status": "ok" if not failed else "verification_failed",
    }


def cleanup_restore_database(
    *,
    db_name: str,
    db_url: str,
    docker_compose_cmd: list[str] | None = None,
) -> dict[str, Any]:
    """Drop a restore-test database after verification.
    
    SAFETY: Only drops databases matching the restore-safe pattern.
    """
    assert_safe_restore_database(db_name)
    
    docker_compose_cmd = docker_compose_cmd or ["docker", "compose"]
    url = make_url(db_url)
    
    drop_cmd = docker_compose_cmd + [
        "exec", "-T", "postgres",
        "psql", "-U", url.username, "-d", "postgres",
        "-c", f'DROP DATABASE IF EXISTS "{db_name}";',
    ]
    result = subprocess.run(
        drop_cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    
    return {
        "db_name": db_name,
        "dropped": result.returncode == 0,
        "error": result.stderr if result.returncode != 0 else None,
    }
