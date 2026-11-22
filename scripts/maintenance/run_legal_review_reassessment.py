"""Phase 10D.3 — Source-change invalidation + reassessment maintenance runner.

Dry-run by default. Does not enable production reviewer endpoints.
Does not schedule cron jobs. Prefer ``lifepilot_test`` for execute mode.

Examples:
  python scripts/maintenance/run_legal_review_reassessment.py --dry-run
  python scripts/maintenance/run_legal_review_reassessment.py --execute --limit 50 --json
  python scripts/maintenance/run_legal_review_reassessment.py --execute --jurisdiction-id 1
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

from sqlalchemy import select  # noqa: E402

from lifepilot.agent.retrieval_policy import reset_freshness_and_review_caches  # noqa: E402
from lifepilot.db.models import DocumentVersion  # noqa: E402
from lifepilot.db.session import get_session_factory  # noqa: E402
from lifepilot.governance.maintenance_alerts import build_freshness_alerts  # noqa: E402
from lifepilot.governance.reassessment import build_reassessment_queue  # noqa: E402
from lifepilot.governance.source_change import (  # noqa: E402
    SourceChangeMateriality,
    classify_source_change_materiality,
    find_stale_pinned_review_items,
    process_source_version_change,
)
from lifepilot.maintenance.pending_changes import load_pending_changes  # noqa: E402
from lifepilot.operations.health import DEFAULT_FRESHNESS_ALERTS, DEFAULT_MAINTENANCE_DIR  # noqa: E402
from lifepilot.operations.maintenance_lock import (  # noqa: E402
    MaintenanceLockError,
    maintenance_lock,
)


def _latest_newer_version(session, document_id: int, previous_version_id: int) -> int | None:
    prev = session.get(DocumentVersion, previous_version_id)
    if prev is None:
        return None
    newer = session.scalars(
        select(DocumentVersion)
        .where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_number > prev.version_number,
        )
        .order_by(DocumentVersion.version_number.desc())
        .limit(1)
    ).first()
    return newer.id if newer else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Invalidate legal-review items affected by source-version changes "
            "and emit reassessment / freshness alert summaries."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Report actions without mutating review state (default).",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Apply source_version_changed invalidations under transaction.",
    )
    parser.add_argument("--limit", type=int, default=100, help="Max stale pins to process.")
    parser.add_argument("--jurisdiction-id", type=int, default=None)
    parser.add_argument("--pathway-id", type=str, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional summary JSON path (defaults under data/runs/knowledge_maintenance/).",
    )
    args = parser.parse_args(argv)
    dry_run = not bool(args.execute)

    factory = get_session_factory()
    summary: dict = {
        "dry_run": dry_run,
        "invalidation_batches": [],
        "counts": {
            "stale_pins_seen": 0,
            "invalidated": 0,
            "skipped": 0,
            "already_invalidated": 0,
            "errors": 0,
            "uncertain": 0,
        },
    }

    try:
        with maintenance_lock("legal_review_reassessment"):
            with factory() as session:
                # 1) Process pending material registry pairs.
                for pending in load_pending_changes().get("items") or []:
                    prev_id = pending.get("previous_version_id")
                    new_id = pending.get("new_version_id")
                    if not prev_id or not new_id:
                        continue
                    mat = classify_source_change_materiality(
                        change_class=pending.get("change_class"),
                        content_hashes_equal=False,
                    )
                    if mat == SourceChangeMateriality.MATERIALITY_UNKNOWN:
                        summary["counts"]["uncertain"] += 1
                    batch = process_source_version_change(
                        session,
                        previous_version_id=int(prev_id),
                        new_version_id=int(new_id),
                        materiality=mat,
                        dry_run=dry_run,
                    )
                    summary["invalidation_batches"].append(batch.to_dict())
                    summary["counts"]["invalidated"] += batch.invalidated_count
                    summary["counts"]["skipped"] += batch.skipped_count
                    summary["counts"]["already_invalidated"] += batch.already_count
                    summary["counts"]["errors"] += sum(
                        1 for r in batch.records if r.outcome == "error"
                    )

                # 2) Stale pins: item still points at non-current version.
                stale = find_stale_pinned_review_items(session)
                if args.jurisdiction_id is not None:
                    stale = [
                        i for i in stale if i.jurisdiction_id == args.jurisdiction_id
                    ]
                if args.pathway_id:
                    stale = [
                        i
                        for i in stale
                        if (i.pathway_id or "") == args.pathway_id.strip()
                    ]
                stale = stale[: max(1, int(args.limit))]
                summary["counts"]["stale_pins_seen"] = len(stale)

                for item in stale:
                    assert item.document_version_id is not None
                    ver = session.get(DocumentVersion, item.document_version_id)
                    if ver is None:
                        continue
                    newer_id = _latest_newer_version(
                        session, ver.document_id, item.document_version_id
                    )
                    if newer_id is None:
                        continue
                    batch = process_source_version_change(
                        session,
                        previous_version_id=item.document_version_id,
                        new_version_id=newer_id,
                        materiality=SourceChangeMateriality.MATERIALITY_UNKNOWN,
                        dry_run=dry_run,
                    )
                    summary["invalidation_batches"].append(batch.to_dict())
                    summary["counts"]["invalidated"] += batch.invalidated_count
                    summary["counts"]["skipped"] += batch.skipped_count
                    summary["counts"]["already_invalidated"] += batch.already_count
                    summary["counts"]["errors"] += sum(
                        1 for r in batch.records if r.outcome == "error"
                    )

                queue = build_reassessment_queue(
                    session,
                    jurisdiction_id=args.jurisdiction_id,
                    pathway_id=args.pathway_id,
                )
                summary["reassessment"] = queue.to_dict()

                inv_records = [
                    r
                    for b in summary["invalidation_batches"]
                    for r in b.get("records") or []
                ]
                alerts = build_freshness_alerts(
                    pending_registry_items=list(
                        load_pending_changes().get("items") or []
                    ),
                    invalidation_records=inv_records,
                    reassessment_entries=queue.to_dict().get("entries") or [],
                    dry_run=dry_run,
                )
                summary["alerts"] = {
                    "pending_material_count": len(
                        alerts.get("pending_material_changes") or []
                    ),
                    "invalidation_alert_count": len(
                        alerts.get("high_risk_review_invalidations") or []
                    ),
                    "overdue_reassessment_count": len(
                        alerts.get("overdue_reassessments") or []
                    ),
                }

                if not dry_run:
                    session.commit()
                    DEFAULT_FRESHNESS_ALERTS.parent.mkdir(parents=True, exist_ok=True)
                    DEFAULT_FRESHNESS_ALERTS.write_text(
                        json.dumps(alerts, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    reset_freshness_and_review_caches()
                else:
                    session.rollback()

    except MaintenanceLockError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1

    out_path = args.out
    if out_path is None:
        DEFAULT_MAINTENANCE_DIR.mkdir(parents=True, exist_ok=True)
        out_path = DEFAULT_MAINTENANCE_DIR / "legal_review_reassessment_latest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(
            f"dry_run={dry_run} counts={summary['counts']} "
            f"reassessment_total={summary['reassessment']['counts']['total']}"
        )
        print(f"wrote {out_path}")

    if summary["counts"]["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
