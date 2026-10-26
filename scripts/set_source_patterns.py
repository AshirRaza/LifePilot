"""Explicitly set allowed_url_patterns for one source (manual scope changes).

Does not crawl. Does not change review_status. Refuses silent scope expansion
unless ``--confirm-expand`` is passed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.db.models import Source  # noqa: E402
from lifepilot.db.session import get_session_factory  # noqa: E402
from lifepilot.registry.review import ReviewError, set_source_allowed_patterns  # noqa: E402
from lifepilot.registry.urls import patterns_expand_scope  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replace allowed_url_patterns for one source after human review. "
            "Use --confirm-expand when widening an approved crawl scope."
        )
    )
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument(
        "--pattern",
        action="append",
        required=True,
        help="Allowlist pattern (repeatable). Replaces the full pattern list.",
    )
    parser.add_argument(
        "--confirm-expand",
        action="store_true",
        help="Required when the new patterns enlarge the previous scope.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the planned change without writing.",
    )
    args = parser.parse_args()

    factory = get_session_factory()
    with factory() as session:
        source = session.get(Source, args.source_id)
        if source is None:
            raise SystemExit(f"source_id={args.source_id} not found")
        before = list(source.allowed_url_patterns or [])
        expands = patterns_expand_scope(before, list(args.pattern))
        print(f"source_id={source.id} name={source.name!r}")
        print(f"review_status={source.review_status}")
        print(f"before={before}")
        print(f"after={list(args.pattern)}")
        print(f"expands_scope={expands}")
        if args.dry_run:
            print("dry-run: no changes written")
            session.rollback()
            return 0
        try:
            updated = set_source_allowed_patterns(
                session,
                source_id=args.source_id,
                patterns=list(args.pattern),
                confirm_scope_expansion=args.confirm_expand,
            )
            session.commit()
        except ReviewError as exc:
            session.rollback()
            raise SystemExit(f"Review failed: {exc}") from exc
        print(f"updated allowed_url_patterns={list(updated.allowed_url_patterns or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
