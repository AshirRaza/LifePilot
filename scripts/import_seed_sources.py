"""Compatibility shim — prefer scripts/database/import_seed_sources.py."""
from __future__ import annotations

import runpy
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "database" / "import_seed_sources.py"
runpy.run_path(str(_TARGET), run_name="__main__")
