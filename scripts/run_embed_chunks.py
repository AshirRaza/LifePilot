"""CLI: embed current document_chunks with local multilingual-e5-small.

Does not implement retrieval, change evidence classifications, or call paid APIs.
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
from lifepilot.rag.embed_pipeline import run_embedding  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Embed current document chunks into document_chunk_embeddings."
    )
    parser.add_argument(
        "--chunk-id",
        type=int,
        action="append",
        dest="chunk_ids",
        help="Limit to chunk id (repeatable)",
    )
    parser.add_argument(
        "--jurisdiction",
        action="append",
        dest="jurisdictions",
        help="Limit to jurisdiction code (repeatable)",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-embed even when an embedding already exists",
    )
    parser.add_argument(
        "--cuda",
        action="store_true",
        help="Use CUDA when available (default: CPU)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    factory = get_session_factory()
    with factory() as session:
        stats = run_embedding(
            session,
            prefer_cuda=bool(args.cuda),
            batch_size=max(1, args.batch_size),
            chunk_ids=args.chunk_ids,
            jurisdiction_codes=args.jurisdictions,
            limit=args.limit,
            force=bool(args.force),
            progress=None if args.json else print,
        )

    payload = stats.as_dict()
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("\nEmbedding complete:")
        for key in (
            "eligible_chunks",
            "embedded",
            "skipped_existing",
            "failed_batches",
            "truncated_chunks",
            "elapsed_seconds",
            "device",
            "model_id",
            "model_revision",
            "dimensions",
        ):
            print(f"  {key}: {payload[key]}")
        print(f"  by_jurisdiction: {payload['by_jurisdiction']}")
        if payload["failures"]:
            print("  failures:")
            for row in payload["failures"][:10]:
                print(f"    - {row}")
    return 1 if stats.failed_batches and stats.embedded == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
