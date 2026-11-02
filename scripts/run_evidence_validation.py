"""Run Phase 6F.0 evidence validation and DB reconciliation (read-only)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.coverage.evidence_validation.run import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
