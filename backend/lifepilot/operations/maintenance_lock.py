"""Cross-process lock so concurrent maintenance runs cannot overlap."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LOCK_PATH = REPO_ROOT / "data" / "locks" / "knowledge_maintenance.lock"


class MaintenanceLockError(RuntimeError):
    """Raised when another maintenance process holds the lock."""


@contextmanager
def maintenance_lock(
    path: Path | None = None,
    *,
    timeout_seconds: float = 0.0,
    stale_after_seconds: float = 6 * 3600,
) -> Iterator[Path]:
    """Acquire an exclusive file lock for knowledge-base maintenance.

    Uses atomic ``O_CREAT|O_EXCL`` lock files (works on Windows and POSIX without
    extra dependencies). Stale locks older than ``stale_after_seconds`` are removed.
    """
    lock_path = path or DEFAULT_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                payload = f"pid={os.getpid()} started={time.time():.3f}\n"
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > stale_after_seconds:
                try:
                    lock_path.unlink(missing_ok=True)
                    continue
                except OSError as exc:
                    raise MaintenanceLockError(
                        f"stale maintenance lock could not be cleared: {lock_path}"
                    ) from exc
            if time.monotonic() >= deadline:
                raise MaintenanceLockError(
                    f"maintenance already running (lock={lock_path})"
                )
            time.sleep(0.2)
    try:
        yield lock_path
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
