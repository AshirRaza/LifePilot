"""Read-only storage integrity audit (raw bytes on host filesystem vs DB on a named volume).

Never deletes, moves or rewrites files. Remediation is a proposal only.
"""

from __future__ import annotations

import hashlib
import random
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifepilot.crawl.storage import data_root, resolve_under_data_root

SAMPLE_RANDOM = 40
SAMPLE_RECENT = 40
SAMPLE_SEED = 20261101


def parse_compose(compose_path: Path) -> dict[str, Any]:
    """Minimal, dependency-free reading of ``compose.yaml`` volume declarations."""
    if not compose_path.is_file():
        return {"present": False}
    lines = compose_path.read_text(encoding="utf-8").splitlines()
    service_volumes: list[str] = []
    top_level_volumes: list[str] = []
    in_service_vols = False
    in_top_vols = False
    for ln in lines:
        stripped = ln.strip()
        if re.match(r"^volumes:\s*$", ln):  # top-level
            in_top_vols, in_service_vols = True, False
            continue
        if re.match(r"^\s+volumes:\s*$", ln):
            in_service_vols, in_top_vols = True, False
            continue
        if in_service_vols:
            if stripped.startswith("- "):
                service_volumes.append(stripped[2:].strip())
            elif stripped and not stripped.startswith("#"):
                in_service_vols = False
        if in_top_vols and re.match(r"^\s+[A-Za-z0-9_]+:\s*$", ln):
            top_level_volumes.append(stripped.rstrip(":"))
    bind_mounts = [v for v in service_volumes if v.startswith(("./", "../", "/", "~"))]
    named = [v for v in service_volumes if v not in bind_mounts]
    return {
        "present": True,
        "service_volume_entries": service_volumes,
        "named_volumes_declared": top_level_volumes,
        "named_volume_mounts": named,
        "bind_mounts": bind_mounts,
        "raw_data_dir_mounted_in_any_container": any("data" in v.split(":")[0] for v in bind_mounts),
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_versions(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT v.id AS version_id, v.document_id, v.version_number, v.storage_path,
                   v.content_sha256, v.byte_size, v.content_type, v.retrieved_at,
                   d.canonical_url, d.content_kind
            FROM document_versions v JOIN documents d ON d.id = v.document_id
            ORDER BY v.id
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def choose_sample(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All PDF/DOCX versions, the most recent N, and a seeded random N of the rest."""
    chosen: dict[int, dict[str, Any]] = {}
    for v in versions:
        path = str(v.get("storage_path") or "").lower()
        if v.get("content_kind") in {"pdf", "docx"} or path.endswith((".pdf", ".docx")):
            chosen[v["version_id"]] = v
    for v in sorted(versions, key=lambda x: x["version_id"], reverse=True)[:SAMPLE_RECENT]:
        chosen[v["version_id"]] = v
    rest = sorted(
        (v for v in versions if v["version_id"] not in chosen), key=lambda x: x["version_id"]
    )
    rng = random.Random(SAMPLE_SEED)
    rng.shuffle(rest)
    for v in rest[:SAMPLE_RANDOM]:
        chosen[v["version_id"]] = v
    return sorted(chosen.values(), key=lambda x: x["version_id"])


def audit_storage(
    session: Session,
    *,
    repo_root: Path,
    storage_root: Path | str | None = None,
    check_docker: bool = True,
) -> dict[str, Any]:
    root = data_root(storage_root)
    versions = load_versions(session)
    missing: list[dict[str, Any]] = []
    escapes: list[dict[str, Any]] = []
    size_mismatch: list[dict[str, Any]] = []
    referenced: set[Path] = set()
    for v in versions:
        sp = v.get("storage_path")
        if not sp:
            missing.append({"version_id": v["version_id"], "reason": "empty_storage_path"})
            continue
        try:
            p = resolve_under_data_root(root, sp)
        except ValueError:
            escapes.append({"version_id": v["version_id"], "storage_path": sp})
            continue
        referenced.add(p.resolve())
        if not p.is_file():
            missing.append({"version_id": v["version_id"], "storage_path": sp, "reason": "file_missing"})
        elif v.get("byte_size") is not None and p.stat().st_size != int(v["byte_size"]):
            size_mismatch.append(
                {
                    "version_id": v["version_id"],
                    "expected": int(v["byte_size"]),
                    "actual": p.stat().st_size,
                }
            )

    sample = choose_sample(versions)
    sha_ok = 0
    sha_bad: list[dict[str, Any]] = []
    sha_unreadable: list[dict[str, Any]] = []
    for v in sample:
        sp = v.get("storage_path")
        if not sp:
            continue
        try:
            p = resolve_under_data_root(root, sp)
            digest = _sha256(p)
        except (OSError, ValueError) as exc:
            sha_unreadable.append({"version_id": v["version_id"], "error": str(exc)})
            continue
        if digest == v["content_sha256"]:
            sha_ok += 1
        else:
            sha_bad.append(
                {"version_id": v["version_id"], "expected": v["content_sha256"], "actual": digest}
            )

    raw_dir = root / "raw" / "documents"
    on_disk = [p.resolve() for p in raw_dir.rglob("*") if p.is_file()] if raw_dir.is_dir() else []
    orphans = sorted(str(p.relative_to(root.resolve())) for p in on_disk if p not in referenced)

    compose = parse_compose(repo_root / "compose.yaml")
    backup_candidates: list[str] = []
    for pattern in ("backup*", "*.dump", "*.sql", "*.sql.gz", "pg_dump*", "restore*"):
        for hit in repo_root.glob(pattern):
            backup_candidates.append(str(hit.relative_to(repo_root)))
    scripts_dir = repo_root / "scripts"
    backup_scripts = [
        str(p.relative_to(repo_root))
        for p in scripts_dir.glob("*")
        if p.is_file() and re.search(r"backup|dump|restore", p.name, re.I)
    ] if scripts_dir.is_dir() else []

    docker: dict[str, Any] = {"checked": False}
    if check_docker:
        try:
            proc = subprocess.run(
                ["docker", "volume", "ls", "--format", "{{.Name}}"],
                capture_output=True, text=True, timeout=15,
            )
            docker = {
                "checked": True,
                "exit_code": proc.returncode,
                "volumes": [x for x in (proc.stdout or "").split() if x][:30],
                "stderr": (proc.stderr or "")[:200],
                "command": "docker volume ls --format {{.Name}} (read-only)",
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            docker = {"checked": True, "error": str(exc)}

    gitignored = False
    gi = repo_root / ".gitignore"
    if gi.is_file():
        gitignored = any(
            ln.strip() in {"data/", "/data/", "data"} for ln in gi.read_text(encoding="utf-8").splitlines()
        )

    findings = [
        {
            "id": "split_storage",
            "severity": "medium",
            "finding": (
                "Raw document bytes are on the host filesystem under data/ while Postgres "
                "lives on the Docker named volume postgres_data. DB rows and files can drift "
                "independently (e.g. volume recreated, or data/ lost)."
            ),
        },
        {
            "id": "no_backup_tooling",
            "severity": "high" if not backup_candidates and not backup_scripts else "low",
            "finding": (
                "No backup/restore scripts or dump files were found in the repo."
                if not backup_candidates and not backup_scripts
                else f"Backup artefacts found: {backup_candidates + backup_scripts}"
            ),
        },
        {
            "id": "data_dir_not_versioned",
            "severity": "medium",
            "finding": (
                "data/ is gitignored; raw bytes (immutable evidence) are not version controlled."
                if gitignored
                else "data/ not confirmed gitignored."
            ),
        },
    ]
    return {
        "phase": "6H",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "data_root": str(root),
        "document_versions": len(versions),
        "files_referenced_by_db": len(referenced),
        "raw_files_on_disk": len(on_disk),
        "missing_files": missing,
        "paths_escaping_root": escapes,
        "size_mismatches": size_mismatch,
        "sha256_sample_size": len(sample),
        "sha256_verified": sha_ok,
        "sha256_mismatches": sha_bad,
        "sha256_unreadable": sha_unreadable,
        "sample_policy": (
            f"all PDF/DOCX versions + {SAMPLE_RECENT} most recent + {SAMPLE_RANDOM} seeded "
            f"random (seed {SAMPLE_SEED})"
        ),
        "orphan_files_not_referenced": orphans[:50],
        "orphan_file_count": len(orphans),
        "integrity_ok": not (missing or escapes or sha_bad or sha_unreadable or size_mismatch),
        "compose": compose,
        "docker_volumes": docker,
        "backup_artifacts_found": backup_candidates + backup_scripts,
        "data_dir_gitignored": gitignored,
        "findings": findings,
        "remediation_proposal_only": [
            "Add a documented backup job: pg_dump (or volume snapshot) plus an archive of data/raw "
            "taken together, with a manifest of version_id -> sha256.",
            "Optionally mount ./data into an app container or move raw bytes to object storage "
            "with versioning; do not migrate in 6H.",
            "Add a verify-storage command that recomputes SHA-256 for all versions on a schedule.",
            "Do not delete or recreate the postgres_data volume; take a dump before any infra change.",
        ],
        "actions_taken": "none (read-only audit; no files modified, no volumes touched)",
    }
