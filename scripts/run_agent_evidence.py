"""CLI: run LangGraph evidence orchestration (no LLM keys required)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from lifepilot.agent import AgentRequest, AgentStatus, run_agent  # noqa: E402


def _print_human(response) -> None:
    print(f"status: {response.status.value}")
    print(f"request_id: {response.request_id}")
    print(f"jurisdiction: {response.jurisdiction}")
    print(f"pathway_id: {response.pathway_id}")
    print(
        "timing_ms: "
        f"total={response.timing.total_ms} "
        f"validate={response.timing.validate_ms} "
        f"evidence={response.timing.evidence_ms} "
        f"internal={response.timing.internal_retrieval_ms} "
        f"external={response.timing.external_retrieval_ms} "
        f"normalize={response.timing.normalize_ms}"
    )
    if response.warnings:
        print("warnings:")
        for w in response.warnings:
            print(f"  - {w}")
    if response.errors:
        print("errors:")
        for e in response.errors:
            print(f"  - {e}")
    if response.missing_information:
        print("missing_information:", ", ".join(response.missing_information))
    print(f"evidence_count: {len(response.evidence)}")
    for i, ev in enumerate(response.evidence, start=1):
        print("-" * 60)
        print(f"[{i}] {ev.source_type.value} | {ev.verification_state.value}")
        print(f"    evidence_id: {ev.evidence_id}")
        print(f"    jurisdiction: {ev.jurisdiction}")
        print(f"    provider: {ev.provider}")
        print(f"    url: {ev.source_url}")
        print(
            f"    ids: doc={ev.document_id} ver={ev.document_version_id} "
            f"chunk={ev.chunk_id}"
        )
        print(f"    score: {ev.retrieval_score}")
        text = ev.passage_text.replace("\n", " ")
        print(f"    passage: {text[:280]}{'…' if len(text) > 280 else ''}")
        print(f"    limitations: {ev.applicability_limitations[:160]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run LifePilot evidence orchestration (internal RAG ± external)."
    )
    parser.add_argument("--question", required=True)
    parser.add_argument("--jurisdiction", required=True)
    parser.add_argument("--pathway", default=None, dest="pathway_id")
    parser.add_argument(
        "--external-mode",
        choices=("auto", "off", "on"),
        default="auto",
        help="auto (default), off (internal-only), on (force connectors).",
    )
    parser.add_argument(
        "--include-external",
        action="store_true",
        help="Legacy alias for --external-mode on.",
    )
    parser.add_argument(
        "--internal-only",
        action="store_true",
        help="Legacy alias for --external-mode off.",
    )
    parser.add_argument(
        "--mode",
        choices=("hybrid", "semantic", "keyword"),
        default="hybrid",
    )
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit full AgentResponse JSON.",
    )
    args = parser.parse_args(argv)

    external_mode = args.external_mode
    include_external = None
    if args.include_external:
        include_external = True
        external_mode = "on"
    elif args.internal_only:
        include_external = False
        external_mode = "off"

    response = run_agent(
        AgentRequest(
            question=args.question,
            jurisdiction=args.jurisdiction,
            pathway_id=args.pathway_id,
            retrieval={
                "mode": args.mode,
                "top_k": args.top_k,
                "external_mode": external_mode,
                "include_external": include_external,
            },
        )
    )

    if args.json:
        print(json.dumps(response.model_dump(mode="json"), indent=2))
    else:
        _print_human(response)

    if response.status == AgentStatus.EXECUTION_FAILURE:
        return 2
    if response.status == AgentStatus.INVALID_REQUEST:
        return 2
    if response.status == AgentStatus.INSUFFICIENT_EVIDENCE:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
