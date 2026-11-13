"""Bounded paid regression for Phase 9C.2 grounding fixes (max 5 calls)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from lifepilot.agent import AgentRequest, run_agent  # noqa: E402
from lifepilot.agent.config import get_agent_config, reset_agent_config_cache  # noqa: E402
from lifepilot.agent.providers import reset_llm_provider_cache  # noqa: E402

OUT = ROOT / "data" / "runs" / "grounded_qa_eval" / "phase_9c2_paid_regression.jsonl"
BENCHMARK = ROOT / "config" / "eval" / "agent_qa_benchmark_v1.json"
BASELINE = ROOT / "data" / "runs" / "grounded_qa_eval" / "baseline_9c1" / "results.jsonl"

# Carefully selected: numbers, weak FR coverage, personalized CEC, injection, salary.
CASE_IDS = ["E01", "E15", "F07", "C05", "S03"]
MAX_COST_USD = 0.25


def main() -> int:
    reset_agent_config_cache()
    reset_llm_provider_cache()
    cfg = get_agent_config()
    if cfg.llm_input_cost_per_mtok <= 0 or cfg.llm_output_cost_per_mtok <= 0:
        raise SystemExit("Pricing env required before paid regression.")
    if cfg.llm_provider != "anthropic":
        raise SystemExit("Anthropic provider required.")

    bench = {c["id"]: c for c in json.loads(BENCHMARK.read_text(encoding="utf-8"))["cases"]}
    baseline = {
        json.loads(l)["question_id"]: json.loads(l)
        for l in BASELINE.read_text(encoding="utf-8").splitlines()
        if l.strip()
    }

    spent = 0.0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for qid in CASE_IDS:
            if spent >= MAX_COST_USD:
                print(f"STOP budget before {qid}")
                break
            case = bench[qid]
            print(f"RUN {qid}…", flush=True)
            resp = run_agent(
                AgentRequest(
                    question=case["question"],
                    jurisdiction=case["jurisdiction"],
                    pathway_id=case.get("pathway_id"),
                    retrieval={"mode": "hybrid", "top_k": 8, "include_external": False},
                    conversation_id=f"9c2_reg_{qid}",
                )
            )
            cost = (resp.usage.estimated_cost_usd if resp.usage else None) or 0.0
            spent += float(cost)
            row = {
                "question_id": qid,
                "agent_status": resp.status.value,
                "answer": resp.answer,
                "claims": [c.model_dump(mode="json") for c in resp.claims],
                "warnings": resp.warnings,
                "missing_information": resp.missing_information,
                "errors": resp.errors,
                "usage": resp.usage.model_dump(mode="json") if resp.usage else None,
                "timing": resp.timing.model_dump(mode="json"),
                "baseline_answer": (baseline.get(qid) or {}).get("answer"),
                "baseline_claim_count": len((baseline.get(qid) or {}).get("claims") or []),
                "revised_claim_count": len(resp.claims),
                "answer_contains_unverified_retrieval": (
                    "unverified retrieval" in (resp.answer or "").lower()
                ),
                "answer_contains_orphaned_fee_guess": (
                    "£1,519" in (resp.answer or "") and qid != "E01"
                ),
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                f"DONE {qid} status={resp.status.value} claims={len(resp.claims)} "
                f"cost=${cost:.4f} spent=${spent:.4f}",
                flush=True,
            )
            if spent > MAX_COST_USD:
                print("STOP: exceeded $0.25 after call", flush=True)
                break
    print(f"Wrote {OUT} total_spent=${spent:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
