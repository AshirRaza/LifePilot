"""CLI for Phase 7 external knowledge retrieval (read-only).

Does not ingest documents, approve sources, or modify coverage readiness.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.external.models import ExternalQuery, ProviderId  # noqa: E402
from lifepilot.external.router import ExternalKnowledgeRouter  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Query external immigration knowledge providers (read-only)."
    )
    parser.add_argument("--query", default="", help="Free-text search query")
    parser.add_argument("--jurisdiction", default=None, help="e.g. GB, CA, US, EU")
    parser.add_argument("--pathway-id", default=None, help="LifePilot pathway id")
    parser.add_argument("--requirement", default=None, help="Critical requirement key")
    parser.add_argument(
        "--provider",
        action="append",
        dest="providers",
        default=None,
        help="Limit to provider id (repeatable)",
    )
    parser.add_argument("--published-after", default=None, help="YYYY-MM-DD")
    parser.add_argument("--published-before", default=None, help="YYYY-MM-DD")
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument(
        "--status",
        action="store_true",
        help="List provider capabilities without querying",
    )
    parser.add_argument(
        "--no-disk-cache",
        action="store_true",
        help="Disable on-disk response cache under data/cache/external",
    )
    args = parser.parse_args(argv)

    with ExternalKnowledgeRouter(enable_disk_cache=not args.no_disk_cache) as router:
        if args.status:
            status = router.provider_status()
            if args.json:
                print(json.dumps(status, indent=2))
            else:
                for row in status:
                    print(
                        f"{row['provider']}: jurisdictions={row['jurisdictions']} "
                        f"capabilities={row['capabilities']}"
                    )
            return 0

        providers = None
        if args.providers:
            try:
                providers = tuple(ProviderId(p) for p in args.providers)
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                print(
                    "Valid providers: "
                    + ", ".join(p.value for p in ProviderId),
                    file=sys.stderr,
                )
                return 2

        result = router.query(
            ExternalQuery(
                query=args.query,
                jurisdiction=args.jurisdiction,
                pathway_id=args.pathway_id,
                requirement=args.requirement,
                providers=providers,
                published_after=args.published_after,
                published_before=args.published_before,
                max_results_per_provider=args.max_results,
            )
        )

        if args.json:
            print(json.dumps(_serialize(result), indent=2, default=str))
            return 0

        print(f"Jurisdiction: {result.query.jurisdiction}")
        print(f"Providers queried: {len(result.outcomes)}")
        for outcome in result.outcomes:
            print(
                f"  - {outcome.provider.value}: {outcome.status.value}"
                + (f" ({outcome.error_detail})" if outcome.error_detail else "")
            )
        print(f"Evidence items: {len(result.items)}")
        for i, item in enumerate(result.items, 1):
            print(f"\n[{i}] {item.provider.value} | {item.source_type.value}")
            print(f"    title: {item.title}")
            print(f"    url: {item.official_url}")
            print(f"    verification: {item.verification_state.value}")
            if item.legal_status:
                print(f"    legal_status: {item.legal_status}")
            if item.passages:
                print(f"    passage: {item.passages[0][:240]}")
        return 0


def _serialize(result) -> dict:
    return {
        "query": {
            "query": result.query.query,
            "jurisdiction": result.query.jurisdiction,
            "pathway_id": result.query.pathway_id,
            "requirement": result.query.requirement,
            "providers": [p.value for p in (result.query.providers or ())],
        },
        "retrieved_at": result.retrieved_at,
        "outcomes": [
            {
                "provider": o.provider.value,
                "status": o.status.value,
                "error_detail": o.error_detail,
                "item_count": len(o.items),
                "capabilities": o.capabilities,
            }
            for o in result.outcomes
        ],
        "items": [
            {
                "provider": i.provider.value,
                "title": i.title,
                "official_url": i.official_url,
                "canonical_url": i.canonical_url,
                "document_id": i.document_id,
                "source_type": i.source_type.value,
                "jurisdiction": i.jurisdiction,
                "pathway_id": i.pathway_id,
                "requirement": i.requirement,
                "passages": i.passages,
                "published_at": i.published_at,
                "updated_at": i.updated_at,
                "effective_at": i.effective_at,
                "language": i.language,
                "license": i.license,
                "legal_status": i.legal_status,
                "verification_state": i.verification_state.value,
                "retrieval_status": i.retrieval_status.value,
                "error_detail": i.error_detail,
                "metadata": i.metadata,
            }
            for i in result.items
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
