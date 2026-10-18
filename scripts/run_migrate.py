"""Apply Alembic migrations (dev helper)."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> None:
    print("Starting migration...", flush=True)
    cfg = Config(str(ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")
    print("Migration complete.", flush=True)


if __name__ == "__main__":
    main()
