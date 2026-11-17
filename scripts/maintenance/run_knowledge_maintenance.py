"""Operational entry point for automated knowledge-base maintenance.

Examples:
  python scripts/maintenance/run_knowledge_maintenance.py --dry-run
  python scripts/maintenance/run_knowledge_maintenance.py --execute --limit 5
  python scripts/maintenance/run_knowledge_maintenance.py --execute --jurisdiction GB --json

Scheduling (examples only; not auto-installed):
  Windows Task Scheduler: daily
    python scripts/maintenance/run_knowledge_maintenance.py --execute --limit 25
  cron:
    15 3 * * * cd /path/LifePilot && .venv/bin/python scripts/maintenance/run_knowledge_maintenance.py --execute --limit 25
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from lifepilot.db.session import get_session_factory  # noqa: E402
from lifepilot.maintenance.pipeline import (  # noqa: E402
    DEFAULT_REPORT,
    default_reindex,
    run_knowledge_maintenance,
)
from lifepilot.operations.maintenance_lock import (  # noqa: E402
    MaintenanceLockError,
    maintenance_lock,
)
from lifepilot.operations.health import (  # noqa: E402
    DEFAULT_FRESHNESS_ALERTS,
    DEFAULT_MAINTENANCE_DIR,
)
from datetime import datetime, timezone  # noqa: E402


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
        default=None,
        help="Write JSON report path (default: data/runs/knowledge_maintenance/).",
    )
    args = parser.parse_args(argv)
    dry_run = not bool(args.execute)

    try:
        with maintenance_lock():
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
    except MaintenanceLockError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    payload = report.to_dict()
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    out_path = args.out
    if out_path is None:
        DEFAULT_MAINTENANCE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = DEFAULT_MAINTENANCE_DIR / f"report_{stamp}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # Stable "latest" pointer for readiness/ops probes.
    latest = DEFAULT_MAINTENANCE_DIR / "latest_report.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # Keep legacy path for older tooling.
    DEFAULT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_REPORT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # Lightweight freshness alert artifact for operators.
    failed = [
        i
        for i in (payload.get("items") or [])
        if (i.get("status") or "") in {"failed", "error", "fetch_failed"}
    ]
    pending = [
        i
        for i in (payload.get("items") or [])
        if (i.get("change_class") or "").startswith("material")
        or (i.get("status") or "") == "queued_for_review"
    ]
    alerts = {
        "generated_at": payload["generated_at"],
        "overdue_sources": failed,
        "pending_material_changes": pending,
        "counts": payload.get("counts"),
        "dry_run": payload.get("dry_run"),
    }
    DEFAULT_FRESHNESS_ALERTS.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_FRESHNESS_ALERTS.write_text(
        json.dumps(alerts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"dry_run={report.dry_run} counts={report.counts}")
        print(f"wrote {out_path}")
        if failed:
            print(f"ALERT: {len(failed)} failed source checks")
        if pending:
            print(f"ALERT: {len(pending)} pending material/review items")
        for item in report.items[:20]:
            print(
                f"  - doc={item.get('document_id')} "
                f"{item.get('status')} class={item.get('change_class')} "
                f"{item.get('canonical_url')}"
            )
        if len(report.items) > 20:
            print(f"  ... {len(report.items) - 20} more")
    return 1 if failed and not dry_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
