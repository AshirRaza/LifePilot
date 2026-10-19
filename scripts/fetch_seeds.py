"""Fetch approved seed HTML pages (dry-run by default).

Does not follow links. Persistence requires ``--persist``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.crawl.fetcher import fetch_approved_seeds  # noqa: E402
from lifepilot.crawl.policy import fetcher_policy  # noqa: E402
from lifepilot.db.session import get_session_factory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch approved registry seed URLs as HTML. "
            "Defaults to dry-run preflight only. "
            "Use --persist to download and store."
        )
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Actually download HTML and write documents/versions + files.",
    )
    parser.add_argument(
        "--source-id",
        type=int,
        default=None,
        help="Fetch a single approved source by id (default: all approved).",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Override storage root (default: ./data or LIFEPILOT_DATA_DIR).",
    )
    args = parser.parse_args()

    policy = fetcher_policy()
    factory = get_session_factory()
    with factory() as session:
        report = fetch_approved_seeds(
            session,
            persist=args.persist,
            source_id=args.source_id,
            policy=policy,
            storage_root=args.data_dir,
        )
        if args.persist:
            session.commit()
        else:
            session.rollback()

    mode = "persist" if args.persist else "dry-run"
    print(f"LifePilot seed fetch ({mode})")
    print(f"user_agent: {policy.user_agent}")
    print(
        f"results: new={report.fetched_new} unchanged={report.unchanged} "
        f"skipped={report.skipped} failed={report.failed} dry_run={report.dry_run}"
    )
    for item in report.results:
        extras = []
        if item.document_id is not None:
            extras.append(f"document_id={item.document_id}")
        if item.version_number is not None:
            extras.append(f"v{item.version_number}")
        if item.storage_path:
            extras.append(f"path={item.storage_path}")
        if item.http_status is not None:
            extras.append(f"http={item.http_status}")
        extra = f" ({', '.join(extras)})" if extras else ""
        print(
            f"  - [{item.status}] id={item.source_id} {item.source_name}: "
            f"{item.seed_url}{extra}"
        )
        if item.message:
            print(f"      {item.message}")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
