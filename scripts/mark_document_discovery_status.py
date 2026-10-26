"""Explicitly set documents.discovery_status (manual content exclusion).

Use this to exclude unsuitable stored HTML (e.g. bot-protection snapshots)
from discovery/downstream processing without rejecting the official source,
deleting files, or rewriting version history.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.db.models import DOCUMENT_DISCOVERY_STATUSES, Document  # noqa: E402
from lifepilot.db.session import get_session_factory  # noqa: E402
from lifepilot.registry.review import (  # noqa: E402
    ReviewError,
    set_document_discovery_status,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Set documents.discovery_status explicitly. "
            "Does not delete files or document_versions. "
            "Does not change sources.review_status."
        )
    )
    parser.add_argument(
        "--document-id",
        type=int,
        action="append",
        required=True,
        help="Document id (repeatable).",
    )
    parser.add_argument(
        "--status",
        required=True,
        choices=sorted(DOCUMENT_DISCOVERY_STATUSES),
        help="New discovery_status value.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned updates without writing.",
    )
    args = parser.parse_args()

    factory = get_session_factory()
    with factory() as session:
        for document_id in args.document_id:
            document = session.get(Document, document_id)
            if document is None:
                raise SystemExit(f"document_id={document_id} not found")
            print(
                f"document_id={document.id} url={document.url_normalized} "
                f"primary_source_id={document.primary_source_id} "
                f"{document.discovery_status!r} -> {args.status!r}"
            )
            if args.dry_run:
                continue
            try:
                set_document_discovery_status(
                    session,
                    document_id=document_id,
                    status=args.status,
                )
            except ReviewError as exc:
                session.rollback()
                raise SystemExit(f"Review failed: {exc}") from exc
        if args.dry_run:
            print("dry-run: no changes written")
            session.rollback()
        else:
            session.commit()
            print("updated discovery_status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
