"""Local filesystem storage for fetched and extracted document artifacts."""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_DATA_DIRNAME = "data"


def data_root(explicit: Path | str | None = None) -> Path:
    """Resolve the LifePilot data root (repo ``data/`` by default)."""
    if explicit is not None:
        return Path(explicit).resolve()
    env = os.environ.get("LIFEPILOT_DATA_DIR")
    if env:
        return Path(env).resolve()
    # backend/lifepilot/crawl/storage.py -> repo root is parents[3]
    repo_root = Path(__file__).resolve().parents[3]
    return (repo_root / DEFAULT_DATA_DIRNAME).resolve()


def resolve_under_data_root(root: Path, storage_path: str) -> Path:
    """Resolve ``storage_path`` and require it to stay under ``root``.

    Uses ``Path.is_relative_to`` so prefix collisions (e.g. ``.../data`` vs
    ``.../database``) cannot bypass the boundary.
    """
    root_resolved = root.resolve()
    candidate = Path(storage_path)
    path = (
        candidate.resolve()
        if candidate.is_absolute()
        else (root_resolved / candidate).resolve()
    )
    if not path.is_relative_to(root_resolved):
        raise ValueError(f"storage_path escapes data root: {storage_path}")
    return path


def safe_read_bytes(root: Path, storage_path: str) -> bytes:
    """Read a stored file after verifying it resolves under ``root``."""
    path = resolve_under_data_root(root, storage_path)
    if not path.is_file():
        raise FileNotFoundError(f"stored file not found: {path}")
    return path.read_bytes()


def version_file_path(
    root: Path,
    document_id: int,
    version_number: int,
    *,
    extension: str = "html",
) -> Path:
    """Absolute path for a document version file (html/pdf/docx)."""
    ext = extension.lstrip(".").lower() or "html"
    if ext not in {"html", "pdf", "docx"}:
        raise ValueError(f"unsupported storage extension: {extension!r}")
    return (
        root
        / "raw"
        / "documents"
        / str(document_id)
        / f"v{version_number}.{ext}"
    )


def extraction_file_path(
    root: Path,
    document_id: int,
    version_number: int,
    *,
    extractor_name: str = "html_structured",
) -> Path:
    """Absolute path for a derived structured extraction JSON file."""
    safe_name = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in extractor_name
    )
    return (
        root
        / "extracted"
        / "documents"
        / str(document_id)
        / f"v{version_number}.{safe_name}.json"
    )


def comparison_file_path(
    root: Path,
    document_id: int,
    previous_version_number: int,
    current_version_number: int,
    *,
    extractor_name: str,
    algorithm_version: str,
    result_revision: int = 1,
) -> Path:
    """Absolute path for a persisted content-comparison JSON file.

    Revision 1 keeps the historical filename. Later revisions append ``.rN`` so
    historical JSON is never overwritten.
    """
    safe_extractor = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in extractor_name
    )
    safe_algo = "".join(
        ch if ch.isalnum() or ch in {"-", "_", "."} else "_"
        for ch in algorithm_version
    )
    base = (
        f"v{previous_version_number}_v{current_version_number}."
        f"{safe_extractor}.cmp{safe_algo}"
    )
    if result_revision > 1:
        base = f"{base}.r{result_revision}"
    return root / "compared" / "documents" / str(document_id) / f"{base}.json"


def relative_storage_path(root: Path, absolute: Path) -> str:
    """Store paths relative to data root when possible."""
    try:
        return absolute.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return absolute.resolve().as_posix()


def write_bytes_atomic(path: Path, content: bytes) -> None:
    """Write bytes via a temp file then replace (no partial visible files)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)


def backup_existing_file(path: Path) -> Path | None:
    """Copy ``path`` to ``path.bak`` if it exists. Return backup path or None."""
    if not path.is_file():
        return None
    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_bytes(path.read_bytes())
    return backup


def restore_from_backup(path: Path, backup: Path | None) -> None:
    """Restore ``path`` from ``backup`` when present; otherwise delete ``path``."""
    if backup is not None and backup.is_file():
        backup.replace(path)
        return
    delete_quiet(path)


def delete_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
