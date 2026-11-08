"""Phase 8F retrieval quality evaluation runner.

Reads gold associations from data/runs/coverage_matrix.csv only.
Does not modify corpus, evidence, or embeddings.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from lifepilot.db.session import get_session_factory  # noqa: E402
from lifepilot.rag.embedder import E5Embedder, EmbedderConfig, resolve_device  # noqa: E402
from lifepilot.rag.eval import (  # noqa: E402
    build_evaluation_cases,
    evaluate_mode,
    measure_performance,
    pathway_association_readiness,
)
from lifepilot.rag.retrieval import InternalKnowledgeRetriever  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate internal retrieval quality.")
    parser.add_argument(
        "--coverage-matrix",
        type=Path,
        default=ROOT / "data" / "runs" / "coverage_matrix.csv",
    )
    parser.add_argument("--cuda", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    factory = get_session_factory()
    with factory() as session:
        cold_t0 = time.perf_counter()
        embedder = E5Embedder(EmbedderConfig(device=resolve_device(bool(args.cuda))))
        # Force model load for cold-start measurement.
        _ = embedder.embed_queries(["warmup"])
        cold_ms = (time.perf_counter() - cold_t0) * 1000

        cases, exclusions = build_evaluation_cases(session, args.coverage_matrix)
        retriever = InternalKnowledgeRetriever(session, embedder=embedder)

        mode_results = {}
        for mode in ("semantic", "keyword", "hybrid"):
            mode_results[mode] = evaluate_mode(
                session, retriever, cases, mode=mode, top_k=10
            ).as_dict()

        pathway = pathway_association_readiness(session, args.coverage_matrix)
        perf = measure_performance(session, retriever)
        perf["cold_model_init_ms"] = round(cold_ms, 2)

        # Baseline counts
        from sqlalchemy import text

        counts = session.execute(
            text(
                """
                SELECT
                  (SELECT COUNT(*) FROM document_chunks WHERE is_current_version) AS chunks,
                  (SELECT COUNT(*) FROM document_chunk_embeddings) AS embeddings,
                  (SELECT COUNT(*) FROM documents) AS documents,
                  (SELECT COUNT(*) FROM sources WHERE review_status='approved') AS approved
                """
            )
        ).mappings().one()

    payload = {
        "baseline": dict(counts),
        "case_count": len(cases),
        "scored_case_count": sum(1 for c in cases if c.scored),
        "exclusions": exclusions,
        "cases": [
            {
                "case_id": c.case_id,
                "query": c.query,
                "jurisdiction": c.jurisdiction,
                "pathway_id": c.pathway_id,
                "requirement": c.requirement,
                "scored": c.scored,
                "category": c.category,
                "expected_document_ids": sorted(c.expected_document_ids),
                "notes": c.notes,
            }
            for c in cases
        ],
        "modes": mode_results,
        "pathway_readiness": pathway,
        "performance": perf,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(json.dumps({k: payload[k] for k in ("baseline", "case_count", "scored_case_count", "exclusions")}, indent=2))
        for mode, metrics in mode_results.items():
            slim = {k: v for k, v in metrics.items() if k != "details"}
            print(f"\n[{mode}]")
            print(json.dumps(slim, indent=2))
        print("\n[pathway_readiness]")
        print(json.dumps(pathway, indent=2))
        print("\n[performance]")
        print(json.dumps(perf, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
