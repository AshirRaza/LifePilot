"""Controlled backfill CLI for historical DocumentVersion chunking & embedding.

Dry-run by default (no DB writes). Never runs automatically on the live corpus.
Deduplicates identical historical content per document and preserves
``is_current_version=False`` on non-current versions.
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
from lifepilot.rag.embed_pipeline import run_embedding  # noqa: E402
from lifepilot.rag.pipeline import iter_eligible_versions, run_chunking  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded historical DocumentVersion chunking and embedding backfill."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Inspect eligible historical versions without writing (default).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Persist chunks and embeddings for selected historical versions.",
    )
    parser.add_argument("--document-id", type=int, action="append", dest="document_ids")
    parser.add_argument("--version-id", type=int, action="append", dest="version_ids")
    parser.add_argument("--jurisdiction", action="append", dest="jurisdictions")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-embed", action="store_true")
    parser.add_argument("--cuda", action="store_true")
    parser.add_argument("--data-dir", type=str, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    dry_run = not bool(args.execute)
    factory = get_session_factory()
    with factory() as session:
        if dry_run:
            versions = iter_eligible_versions(
                session,
                document_ids=args.document_ids,
                version_ids=args.version_ids,
                jurisdiction_codes=args.jurisdictions,
                limit=args.limit,
                include_historical=True,
            )
            payload = {
                "dry_run": True,
                "eligible_versions": len(versions),
                "non_current_versions": sum(1 for v in versions if not v.is_current),
                "versions": [
                    {
                        "version_id": v.id,
                        "document_id": v.document_id,
                        "version_number": v.version_number,
                        "is_current": v.is_current,
                    }
                    for v in versions
                ],
            }
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(
                    f"dry-run: eligible_versions={payload['eligible_versions']} "
                    f"non_current={payload['non_current_versions']}"
                )
            return 0

        chunk_stats = run_chunking(
            session,
            data_dir=args.data_dir,
            document_ids=args.document_ids,
            version_ids=args.version_ids,
            jurisdiction_codes=args.jurisdictions,
            limit=args.limit,
            batch_size=max(1, args.batch_size),
            force=bool(args.force),
            include_historical=True,
            dedupe_historical=True,
            progress=None if args.json else print,
        )
        embed_stats = None
        if not args.no_embed:
            embed_stats = run_embedding(
                session,
                prefer_cuda=bool(args.cuda),
                batch_size=max(1, args.batch_size),
                jurisdiction_codes=args.jurisdictions,
                limit=None,
                force=bool(args.force),
                include_historical=True,
                progress=None if args.json else print,
            )

    summary = {
        "dry_run": False,
        "chunking": chunk_stats.as_dict(),
        "embedding": None if embed_stats is None else embed_stats.as_dict(),
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print("Historical backfill complete:", summary["chunking"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
