"""Phase 9D.3 bounded end-to-end agent eval (external_mode=auto, max ~$3).

Uses the normal production agent + unified verifier. Does not retune on X16–X20.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from lifepilot.agent.config import AgentConfig, get_agent_config, reset_agent_config_cache
from lifepilot.agent.contracts import AgentRequest
from lifepilot.agent.graph import reset_compiled_agent_graph
from lifepilot.agent.runner import run_agent
from lifepilot.evaluation.accounting import estimate_usage_cost_usd

OUT_DIR = ROOT / "data" / "runs" / "verification_reliability_e2e"
RESULTS_PATH = OUT_DIR / "results.jsonl"
SUMMARY_PATH = OUT_DIR / "summary.json"

# ~12 representative questions; excludes held-out X16–X20.
TARGET_IDS = [
    "F07",
    "X05",
    "X06",
    "E15",
    "X01",
    "X02",
    "X04",
    "S05",
    "X14",
    "F01",
    "X03",
    "X07",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_cases() -> list[dict[str, Any]]:
    v1 = json.loads(
        (ROOT / "config/eval/agent_qa_benchmark_v1.json").read_text(encoding="utf-8")
    )
    supp = json.loads(
        (ROOT / "config/eval/agent_qa_supplemental_9d1.json").read_text(encoding="utf-8")
    )
    by_id: dict[str, dict[str, Any]] = {}
    for c in v1.get("cases") or v1.get("questions") or []:
        cid = c.get("id") or c.get("question_id")
        if cid:
            by_id[str(cid)] = {**c, "benchmark_source": "original_v1"}
    for c in supp.get("cases") or []:
        cid = c.get("id")
        if cid and not c.get("held_out"):
            by_id[str(cid)] = {**c, "benchmark_source": "supplemental_9d1"}
    return [by_id[i] for i in TARGET_IDS if i in by_id]


def _estimate_cost(usage: dict[str, Any] | None, cfg) -> float:
    return estimate_usage_cost_usd(
        usage,
        input_cost_per_mtok=float(cfg.llm_input_cost_per_mtok or 2.0),
        output_cost_per_mtok=float(cfg.llm_output_cost_per_mtok or 10.0),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-cost-usd", type=float, default=3.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    reset_agent_config_cache()
    reset_compiled_agent_graph()
    cfg = get_agent_config()
    cases = _load_cases()
    print(
        f"cases={len(cases)} max_cost={args.max_cost_usd} "
        f"llm_configured={bool(cfg.llm_configured)}"
    )
    if args.dry_run:
        for c in cases:
            print(f"  - {c['id']} {c.get('jurisdiction')} {c.get('pathway_id')}")
        return 0

    if not cfg.llm_configured:
        print("LIMITATION: LLM not configured; skipping live E2E.")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        SUMMARY_PATH.write_text(
            json.dumps(
                {
                    "generated_at": _utc_now(),
                    "n": 0,
                    "spent_usd": 0.0,
                    "max_cost_usd": args.max_cost_usd,
                    "limitation": "llm_not_configured",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if RESULTS_PATH.exists():
        RESULTS_PATH.unlink()

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
                conversation_id=f"9d3_{case['id']}",
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
        ver = resp.verification or {}
        row = {
            "question_id": case["id"],
            "benchmark_source": case.get("benchmark_source"),
            "jurisdiction": case.get("jurisdiction"),
            "pathway_id": case.get("pathway_id"),
            "question": case.get("question"),
            "evaluated_at": _utc_now(),
            "agent_status": resp.status.value,
            "answer": resp.answer,
            "claims": [c.model_dump(mode="json") for c in resp.claims],
            "pre_verification_claims": [
                c.model_dump(mode="json") for c in resp.pre_verification_claims
            ],
            "verification": ver,
            "warnings": resp.warnings,
            "missing_information": resp.missing_information,
            "retrieval_decision": resp.retrieval_decision,
            "timing_ms": resp.timing.model_dump(mode="json"),
            "wall_ms": wall,
            "usage": usage,
            "estimated_cost_usd": cost,
            "spent_usd_cumulative": round(spent, 6),
        }
        rows.append(row)
        with RESULTS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            f"RUN {case['id']} status={resp.status.value} "
            f"claims={len(resp.claims)} "
            f"suppressed={len(ver.get('suppressed_claims') or [])} "
            f"ver_ms={resp.timing.verification_ms} "
            f"cost={cost:.4f} spent={spent:.4f}"
        )

    summary = {
        "generated_at": _utc_now(),
        "n": len(rows),
        "spent_usd": round(spent, 6),
        "max_cost_usd": args.max_cost_usd,
        "by_id": {
            r["question_id"]: {
                "status": r["agent_status"],
                "claim_n": len(r.get("claims") or []),
                "suppressed": len(
                    (r.get("verification") or {}).get("suppressed_claims") or []
                ),
                "verification_ms": (r.get("timing_ms") or {}).get("verification_ms"),
                "wall_ms": r.get("wall_ms"),
                "retrieval_decision": r.get("retrieval_decision"),
                "cost_usd": r.get("estimated_cost_usd"),
            }
            for r in rows
        },
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"DONE n={len(rows)} spent={spent:.4f} out={OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
