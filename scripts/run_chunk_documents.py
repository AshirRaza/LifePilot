"""CLI: clean and chunk current successful extractions into document_chunks.

Does not generate embeddings, change evidence classifications, or modify
source approvals. Idempotent: unchanged versions are skipped unless --force.
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
from lifepilot.rag.pipeline import run_chunking  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Chunk current document extractions into document_chunks."
    )
    parser.add_argument(
        "--document-id",
        type=int,
        action="append",
        dest="document_ids",
        help="Limit to document id (repeatable)",
    )
    parser.add_argument(
        "--jurisdiction",
        action="append",
        dest="jurisdictions",
        help="Limit to jurisdiction code (repeatable), e.g. GB",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max versions to scan")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="Commit every N versions (default 25)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild chunks even when fingerprints match",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Override data root (default: repo data/)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON summary")
    args = parser.parse_args(argv)

    factory = get_session_factory()
    with factory() as session:
        stats = run_chunking(
            session,
            data_dir=args.data_dir,
            document_ids=args.document_ids,
            jurisdiction_codes=args.jurisdictions,
            limit=args.limit,
            batch_size=max(1, args.batch_size),
            force=bool(args.force),
            progress=None if args.json else print,
        )

    payload = stats.as_dict()
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("\nChunking complete:")
        for key in (
            "eligible_versions",
            "processed",
            "skipped_unchanged",
            "skipped_no_extraction",
            "skipped_missing_file",
            "skipped_empty_chunks",
            "skipped_no_jurisdiction",
            "failed",
            "chunks_written",
            "chunks_deleted",
        ):
            print(f"  {key}: {payload[key]}")
        print(f"  chunk_length: {payload['chunk_length']}")
        print(f"  by_jurisdiction: {payload['by_jurisdiction']}")
        if payload["failures"]:
            print(f"  failures (first {len(payload['failures'])}):")
            for row in payload["failures"][:10]:
                print(f"    - {row}")
    return 1 if stats.failed and stats.processed == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
