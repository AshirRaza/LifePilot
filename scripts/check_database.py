"""Compatibility shim — prefer scripts/database/check_database.py."""
from __future__ import annotations

import runpy
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "database" / "check_database.py"
runpy.run_path(str(_TARGET), run_name="__main__")
