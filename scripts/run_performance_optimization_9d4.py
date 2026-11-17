"""Phase 9D.4 paired performance benchmark (external_mode=auto).

Captures stage latencies, tokens, cost, retrieval decisions, and verification
outcomes for a fixed case list. Use --phase baseline|after.
Budget-capped; does not tune on X16–X20.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from lifepilot.agent.config import AgentConfig, get_agent_config, reset_agent_config_cache
from lifepilot.agent.contracts import AgentRequest
from lifepilot.agent.graph import reset_compiled_agent_graph
from lifepilot.agent.orchestration import reset_evidence_orchestrator_override
from lifepilot.agent.runner import run_agent
from lifepilot.eval.accounting import estimate_usage_cost_usd

OUT_DIR = ROOT / "data" / "runs" / "performance_optimization_9d4"
CASES_PATH = ROOT / "config" / "eval" / "performance_cases_9d4.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_case_defs() -> list[dict[str, Any]]:
    meta = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    wanted = {c["id"]: c for c in meta.get("cases") or []}
    v1 = json.loads(
        (ROOT / "config/eval/agent_qa_benchmark_v1.json").read_text(encoding="utf-8")
    )
    supp = json.loads(
        (ROOT / "config/eval/agent_qa_supplemental_9d1.json").read_text(encoding="utf-8")
    )
    by_id: dict[str, dict[str, Any]] = {}
    for c in v1.get("cases") or v1.get("questions") or []:
        cid = str(c.get("id") or c.get("question_id") or "")
        if cid in wanted:
            by_id[cid] = {**c, **wanted[cid], "benchmark_source": "original_v1"}
    for c in supp.get("cases") or []:
        cid = str(c.get("id") or "")
        if cid in wanted and not c.get("held_out"):
            by_id[cid] = {**c, **wanted[cid], "benchmark_source": "supplemental_9d1"}
    order = [c["id"] for c in meta.get("cases") or []]
    return [by_id[i] for i in order if i in by_id]


def _estimate_cost(usage: dict[str, Any] | None, cfg) -> float:
    return estimate_usage_cost_usd(
        usage,
        input_cost_per_mtok=float(cfg.llm_input_cost_per_mtok or 2.0),
        output_cost_per_mtok=float(cfg.llm_output_cost_per_mtok or 10.0),
    )


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    k = (len(ordered) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def _summarize(rows: list[dict[str, Any]], *, phase: str, spent: float, max_cost: float) -> dict:
    def col(key: str) -> list[float]:
        out: list[float] = []
        for r in rows:
            v = r.get(key)
            if isinstance(v, (int, float)):
                out.append(float(v))
        return out

    timing_keys = [
        "wall_ms",
        "total_ms",
        "evidence_ms",
        "internal_retrieval_ms",
        "external_retrieval_ms",
        "llm_ms",
        "grounding_ms",
        "verification_ms",
    ]
    stages: dict[str, Any] = {}
    for key in timing_keys:
        vals = col(key)
        stages[key] = {
            "n": len(vals),
            "median": None if not vals else round(statistics.median(vals), 2),
            "p95": None if not vals else round(_percentile(vals, 95) or 0.0, 2),
            "mean": None if not vals else round(statistics.mean(vals), 2),
        }

    external_runs = sum(1 for r in rows if r.get("ran_external"))
    return {
        "generated_at": _utc_now(),
        "phase": phase,
        "n": len(rows),
        "spent_usd": round(spent, 6),
        "max_cost_usd": max_cost,
        "stages_ms": stages,
        "tokens": {
            "input_median": (
                None
                if not col("input_tokens")
                else statistics.median(col("input_tokens"))
            ),
            "output_median": (
                None
                if not col("output_tokens")
                else statistics.median(col("output_tokens"))
            ),
            "input_total": int(sum(col("input_tokens"))),
            "output_total": int(sum(col("output_tokens"))),
        },
        "cost": {
            "per_answer_median": (
                None
                if not col("estimated_cost_usd")
                else round(statistics.median(col("estimated_cost_usd")), 6)
            ),
            "total": round(spent, 6),
        },
        "connector": {
            "external_invocation_rate": (
                None if not rows else round(external_runs / len(rows), 4)
            ),
            "external_runs": external_runs,
        },
        "evidence": {
            "final_count_median": (
                None
                if not col("evidence_count")
                else statistics.median(col("evidence_count"))
            ),
            "prompt_evidence_median": (
                None
                if not col("prompt_evidence_count")
                else statistics.median(col("prompt_evidence_count"))
            ),
        },
        "by_id": {
            r["question_id"]: {
                "status": r.get("agent_status"),
                "wall_ms": r.get("wall_ms"),
                "llm_ms": r.get("llm_ms"),
                "evidence_ms": r.get("evidence_ms"),
                "external_retrieval_ms": r.get("external_retrieval_ms"),
                "verification_ms": r.get("verification_ms"),
                "input_tokens": r.get("input_tokens"),
                "output_tokens": r.get("output_tokens"),
                "cost_usd": r.get("estimated_cost_usd"),
                "retrieval_decision": r.get("retrieval_decision"),
                "ran_external": r.get("ran_external"),
                "evidence_count": r.get("evidence_count"),
                "suppressed": r.get("suppressed_count"),
            }
            for r in rows
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["baseline", "after"], required=True)
    parser.add_argument("--max-cost-usd", type=float, default=1.5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Optional case cap")
    args = parser.parse_args(argv)

    reset_agent_config_cache()
    reset_compiled_agent_graph()
    reset_evidence_orchestrator_override()
    cfg = get_agent_config()
    cases = _load_case_defs()
    if args.limit > 0:
        cases = cases[: args.limit]

    print(
        f"phase={args.phase} cases={len(cases)} max_cost={args.max_cost_usd} "
        f"llm_configured={bool(cfg.llm_configured)}"
    )
    if args.dry_run:
        for c in cases:
            print(f"  - {c['id']} {c.get('jurisdiction')} {c.get('category')}")
        return 0

    if not cfg.llm_configured:
        print("LIMITATION: LLM not configured")
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUT_DIR / f"results_{args.phase}.jsonl"
    summary_path = OUT_DIR / f"summary_{args.phase}.json"
    if results_path.exists():
        results_path.unlink()

    spent = 0.0
    rows: list[dict[str, Any]] = []
    for case in cases:
        if spent >= args.max_cost_usd:
            print(f"STOP budget spent={spent:.4f}")
            break
        started = time.perf_counter()
        resp = run_agent(
            AgentRequest(
                question=case["question"],
                jurisdiction=case["jurisdiction"],
                pathway_id=case.get("pathway_id"),
                retrieval={"mode": "hybrid", "top_k": 8, "external_mode": "auto"},
                conversation_id=f"9d4_{args.phase}_{case['id']}",
            )
        )
        wall = round((time.perf_counter() - started) * 1000.0, 2)
        usage = resp.usage.model_dump(mode="json") if resp.usage else {}
        cost_cfg = cfg
        if (cfg.llm_input_cost_per_mtok or 0) <= 0:
            cost_cfg = AgentConfig(
                llm_input_cost_per_mtok=2.0,
                llm_output_cost_per_mtok=10.0,
            )
        cost = _estimate_cost(usage, cost_cfg)
        spent += cost
        timing = resp.timing.model_dump(mode="json") if resp.timing else {}
        ver = resp.verification or {}
        decision = resp.retrieval_decision or ""
        ran_external = decision in {
            "internal_plus_external",
            "external_verification_required",
        } and float(timing.get("external_retrieval_ms") or 0) > 0
        # Count external attempt even if 0ms cached / empty
        if decision in {
            "internal_plus_external",
            "external_verification_required",
        }:
            ran_external = True

        row = {
            "phase": args.phase,
            "question_id": case["id"],
            "category": case.get("category"),
            "jurisdiction": case.get("jurisdiction"),
            "pathway_id": case.get("pathway_id"),
            "question": case.get("question"),
            "evaluated_at": _utc_now(),
            "agent_status": resp.status.value,
            "answer": resp.answer,
            "retrieval_decision": decision,
            "ran_external": ran_external,
            "evidence_count": len(resp.evidence or []),
            "prompt_evidence_count": min(len(resp.evidence or []), cfg.llm_max_evidence_items),
            "claims_count": len(resp.claims or []),
            "suppressed_count": len(ver.get("suppressed_claims") or []),
            "verification": ver,
            "warnings": resp.warnings,
            "missing_information": resp.missing_information,
            "wall_ms": wall,
            "total_ms": timing.get("total_ms"),
            "evidence_ms": timing.get("evidence_ms"),
            "internal_retrieval_ms": timing.get("internal_retrieval_ms"),
            "external_retrieval_ms": timing.get("external_retrieval_ms"),
            "normalize_ms": timing.get("normalize_ms"),
            "llm_ms": timing.get("llm_ms"),
            "grounding_ms": timing.get("grounding_ms"),
            "verification_ms": timing.get("verification_ms"),
            "answer_ms": timing.get("answer_ms"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "estimated_cost_usd": cost,
            "spent_usd_cumulative": round(spent, 6),
            "usage": usage,
            "timing_ms": timing,
        }
        rows.append(row)
        with results_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            f"RUN {case['id']} status={resp.status.value} wall={wall:.0f}ms "
            f"llm={timing.get('llm_ms')} ev={timing.get('evidence_ms')} "
            f"ext={timing.get('external_retrieval_ms')} "
            f"in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
            f"dec={decision} cost={cost:.4f} spent={spent:.4f}"
        )

    summary = _summarize(rows, phase=args.phase, spent=spent, max_cost=args.max_cost_usd)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"DONE phase={args.phase} n={len(rows)} spent={spent:.4f} out={OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
