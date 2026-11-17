"""Concurrent maintenance lock regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from lifepilot.operations.maintenance_lock import MaintenanceLockError, maintenance_lock


def test_maintenance_lock_blocks_concurrent(tmp_path: Path):
    lock = tmp_path / "maint.lock"
    with maintenance_lock(lock):
        with pytest.raises(MaintenanceLockError):
            with maintenance_lock(lock, timeout_seconds=0.0):
                pass
    # After release, lock can be acquired again.
    with maintenance_lock(lock):
        assert lock.is_file()


def test_stale_lock_cleared(tmp_path: Path):
    lock = tmp_path / "stale.lock"
    lock.write_text("pid=1\n", encoding="utf-8")
    # Pretend stale by using tiny stale_after.
    import os
    import time

    os.utime(lock, (time.time() - 10, time.time() - 10))
    with maintenance_lock(lock, stale_after_seconds=1.0):
        assert lock.is_file()
