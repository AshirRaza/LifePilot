"""CLI: ask LifePilot a grounded immigration question (Claude when configured)."""

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
from lifepilot.agent.providers import anthropic_api_key_present, get_llm_provider  # noqa: E402


def _print_human(response) -> None:
    print(f"status: {response.status.value}")
    print(f"request_id: {response.request_id}")
    print(f"jurisdiction: {response.jurisdiction}")
    print(f"pathway_id: {response.pathway_id}")
    if response.retrieval_decision:
        print(f"retrieval_decision: {response.retrieval_decision}")
    print(
        "timing_ms: "
        f"total={response.timing.total_ms} "
        f"evidence={response.timing.evidence_ms} "
        f"llm={response.timing.llm_ms} "
        f"grounding={response.timing.grounding_ms} "
        f"answer={response.timing.answer_ms}"
    )
    if response.usage:
        print(
            "usage: "
            f"provider={response.usage.provider} model={response.usage.model} "
            f"in={response.usage.input_tokens} out={response.usage.output_tokens} "
            f"cost_usd={response.usage.estimated_cost_usd}"
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
    print()
    if response.answer:
        print("answer:")
        print(response.answer)
    else:
        print("answer: (none)")
    if response.claims:
        print()
        print("claims:")
        evidence_by_id = {e.evidence_id: e for e in response.evidence}
        for i, claim in enumerate(response.claims, start=1):
            print(f"  [{i}] {claim.claim}")
            if claim.uncertainty:
                print(f"      uncertainty: {claim.uncertainty}")
            for eid in claim.evidence_ids:
                ev = evidence_by_id.get(eid)
                url = ev.source_url if ev else None
                print(f"      cite: {eid} → {url}")
    print()
    print(f"evidence_count: {len(response.evidence)}")
    for i, ev in enumerate(response.evidence, start=1):
        print(
            f"  [{i}] {ev.evidence_id} | {ev.verification_state.value} | {ev.source_url}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ask LifePilot an evidence-grounded immigration question. "
            "Requires ANTHROPIC_API_KEY + LIFEPILOT_LLM_PROVIDER=anthropic for answers; "
            "otherwise returns evidence only."
        )
    )
    parser.add_argument("--question", required=True)
    parser.add_argument("--jurisdiction", required=True)
    parser.add_argument("--pathway", default=None, dest="pathway_id")
    parser.add_argument(
        "--external-mode",
        choices=("auto", "off", "on"),
        default="auto",
        help=(
            "Retrieval policy: auto (default intelligent hybrid), "
            "off (internal-only), on (always combine official connectors)."
        ),
    )
    parser.add_argument(
        "--include-external",
        action="store_true",
        help="Legacy alias for --external-mode on.",
    )
    parser.add_argument(
        "--internal-only",
        action="store_true",
        help="Legacy alias for --external-mode off (reproducible evals).",
    )
    parser.add_argument(
        "--mode",
        choices=("hybrid", "semantic", "keyword"),
        default="hybrid",
    )
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--evidence-only",
        action="store_true",
        help="Skip LLM even if Anthropic is configured (retrieval only).",
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

    retrieval = {
        "mode": args.mode,
        "top_k": args.top_k,
        "external_mode": external_mode,
        "include_external": include_external,
    }

    if args.evidence_only:
        from dataclasses import replace

        from lifepilot.agent.config import get_agent_config

        cfg = replace(get_agent_config(), llm_provider="", llm_model="")
        response = run_agent(
            AgentRequest(
                question=args.question,
                jurisdiction=args.jurisdiction,
                pathway_id=args.pathway_id,
                retrieval=retrieval,
            ),
            config=cfg,
        )
    else:
        provider = get_llm_provider()
        if provider.provider_id == "disabled" and not anthropic_api_key_present():
            print(
                "note: Anthropic not configured; returning evidence without an LLM answer.",
                file=sys.stderr,
            )
        response = run_agent(
            AgentRequest(
                question=args.question,
                jurisdiction=args.jurisdiction,
                pathway_id=args.pathway_id,
                retrieval=retrieval,
            )
        )

    if args.json:
        print(json.dumps(response.model_dump(mode="json"), indent=2))
    else:
        _print_human(response)

    if response.status in {
        AgentStatus.EXECUTION_FAILURE,
        AgentStatus.INVALID_REQUEST,
    }:
        return 2
    if response.status == AgentStatus.INSUFFICIENT_EVIDENCE:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
