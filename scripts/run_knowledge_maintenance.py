"""Compatibility shim — prefer scripts/maintenance/run_knowledge_maintenance.py."""
from __future__ import annotations

import runpy
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "maintenance" / "run_knowledge_maintenance.py"
runpy.run_path(str(_TARGET), run_name="__main__")
