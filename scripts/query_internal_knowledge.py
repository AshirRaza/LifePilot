"""CLI: query internal document chunks (semantic / keyword / hybrid).

Does not generate answers, change evidence classifications, or invent pathway
associations. Results are always verification_state=unverified_retrieval.
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
from lifepilot.rag.retrieval import (  # noqa: E402
    InternalKnowledgeRetriever,
    InternalQuery,
    RetrievalStatus,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Query internal LifePilot document chunks (no answer generation)."
    )
    parser.add_argument("--query", required=True, help="Search query text")
    parser.add_argument(
        "--jurisdiction",
        required=True,
        help="Mandatory jurisdiction code (e.g. GB). Never expanded silently.",
    )
    parser.add_argument(
        "--mode",
        choices=("semantic", "keyword", "hybrid"),
        default="hybrid",
    )
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument(
        "--pathway-id",
        default=None,
        help="Optional curated pathway filter (unsupported until associations exist)",
    )
    parser.add_argument(
        "--requirement",
        default=None,
        help="Optional curated requirement filter (unsupported until associations exist)",
    )
    parser.add_argument("--cuda", action="store_true", help="Prefer CUDA for query encoding")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    factory = get_session_factory()
    with factory() as session:
        retriever = InternalKnowledgeRetriever(session, prefer_cuda=bool(args.cuda))
        result = retriever.retrieve(
            InternalQuery(
                query=args.query,
                jurisdiction=args.jurisdiction,
                mode=args.mode,
                top_k=args.top_k,
                pathway_id=args.pathway_id,
                requirement=args.requirement,
            )
        )

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(
            f"status={result.status.value} mode={result.mode} "
            f"jurisdiction={result.jurisdiction} latency_ms={result.latency_ms:.1f}"
        )
        if result.message:
            print(f"message: {result.message}")
        for w in result.warnings:
            print(f"warning: {w}")
        for i, p in enumerate(result.passages, start=1):
            print(
                f"\n[{i}] score={p.score:.4f} jur={p.jurisdiction_code} "
                f"chunk={p.chunk_id} doc={p.document_id}"
            )
            print(f"    url: {p.official_url}")
            if p.heading_path:
                print(f"    heading: {p.heading_path}")
            print(f"    text: {p.content_text[:280].replace(chr(10), ' ')}")
            print(f"    {p.applicability_warning}")

    if result.status == RetrievalStatus.INVALID_REQUEST:
        return 2
    if result.status == RetrievalStatus.UNSUPPORTED_FILTER:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
