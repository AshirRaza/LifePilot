"""Compatibility shim — prefer scripts/operations/ask_lifepilot.py."""
from __future__ import annotations

import runpy
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "operations" / "ask_lifepilot.py"
runpy.run_path(str(_TARGET), run_name="__main__")
