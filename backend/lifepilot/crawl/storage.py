"""Local filesystem storage for fetched HTML seed pages."""

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


def version_file_path(
    root: Path,
    document_id: int,
    version_number: int,
) -> Path:
    """Absolute path for a document version HTML file."""
    return (
        root
        / "raw"
        / "documents"
        / str(document_id)
        / f"v{version_number}.html"
    )


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


def delete_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
