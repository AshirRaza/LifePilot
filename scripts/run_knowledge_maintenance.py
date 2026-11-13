"""Operational entry point for automated knowledge-base maintenance.

Examples:
  python scripts/run_knowledge_maintenance.py --dry-run
  python scripts/run_knowledge_maintenance.py --execute --limit 5
  python scripts/run_knowledge_maintenance.py --execute --jurisdiction GB --json

Scheduling (examples only; not auto-installed):
  Windows Task Scheduler: daily
    python scripts/run_knowledge_maintenance.py --execute --limit 25
  cron:
    15 3 * * * cd /path/LifePilot && .venv/bin/python scripts/run_knowledge_maintenance.py --execute --limit 25
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from lifepilot.db.session import get_session_factory  # noqa: E402
from lifepilot.maintenance.pipeline import (  # noqa: E402
    DEFAULT_REPORT,
    default_reindex,
    run_knowledge_maintenance,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recheck due official sources, classify changes, process safe "
            "editorial updates, and queue material/uncertain legal changes."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Plan only (default).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform fetches and DB updates.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--jurisdiction", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--no-reindex",
        action="store_true",
        help="Skip extract/chunk for editorial promotions.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_REPORT,
        help="Write JSON report path.",
    )
    args = parser.parse_args(argv)
    dry_run = not bool(args.execute)

    factory = get_session_factory()
    with factory() as session:
        report = run_knowledge_maintenance(
            session,
            dry_run=dry_run,
            limit=args.limit,
            jurisdiction=args.jurisdiction,
            process_editorial=not args.no_reindex,
            reindex_fn=None if (dry_run or args.no_reindex) else default_reindex,
        )
        if not dry_run:
            session.commit()
        else:
            session.rollback()

    payload = report.to_dict()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"dry_run={report.dry_run} counts={report.counts}")
        print(f"wrote {args.out}")
        for item in report.items[:20]:
            print(
                f"  - doc={item.get('document_id')} "
                f"{item.get('status')} class={item.get('change_class')} "
                f"{item.get('canonical_url')}"
            )
        if len(report.items) > 20:
            print(f"  ... {len(report.items) - 20} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
