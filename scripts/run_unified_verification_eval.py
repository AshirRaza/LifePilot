"""Phase 9D.2 targeted real-answer evaluation through the main agent.

Runs a representative subset (incl. F07/X05/X06/E15) with unified verification
enabled via the normal LangGraph path. Budget-capped; does not rewrite 9D.1 artifacts.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from lifepilot.agent.config import get_agent_config, reset_agent_config_cache
from lifepilot.agent.contracts import AgentRequest, AgentStatus
from lifepilot.agent.graph import reset_compiled_agent_graph
from lifepilot.agent.runner import run_agent
from lifepilot.eval.accounting import estimate_usage_cost_usd

OUT_DIR = ROOT / "data" / "runs" / "unified_verification_eval"
REPORT_PATH = ROOT / "data" / "runs" / "phase_9d2_unified_legal_verification_report.md"
RESULTS_PATH = OUT_DIR / "results.jsonl"

# Representative subset — excludes held-out X16–X20.
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
            by_id[cid] = {**c, "benchmark_source": "original_v1"}
    for c in supp.get("cases") or []:
        cid = c.get("id")
        if cid and not c.get("held_out"):
            by_id[cid] = {**c, "benchmark_source": "supplemental_9d1"}
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
    print(f"cases={len(cases)} max_cost={args.max_cost_usd} key={bool(cfg.llm_configured)}")
    if args.dry_run:
        for c in cases:
            print(f"  - {c['id']} {c.get('jurisdiction')} {c.get('pathway_id')}")
        return 0

    if not cfg.llm_configured:
        print("BLOCKER: LLM not configured; offline preparation only.")
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
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
                conversation_id=f"9d2_{case['id']}",
            )
        )
        wall = round((time.perf_counter() - started) * 1000.0, 2)
        usage = resp.usage.model_dump(mode="json") if resp.usage else {}
        # Prefer configured pricing; fall back to eval defaults for accounting.
        cost_cfg = cfg
        if (cfg.llm_input_cost_per_mtok or 0) <= 0:
            from lifepilot.agent.config import AgentConfig

            cost_cfg = AgentConfig(
                llm_input_cost_per_mtok=2.0,
                llm_output_cost_per_mtok=10.0,
            )
        cost = _estimate_cost(usage, cost_cfg)
        spent += cost
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
            "verification": resp.verification,
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
        ver = resp.verification or {}
        print(
            f"RUN {case['id']} status={resp.status.value} "
            f"claims={len(resp.claims)} suppressed={len(ver.get('suppressed_claims') or [])} "
            f"ver_ms={resp.timing.verification_ms} cost={cost:.4f} spent={spent:.4f}"
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
                "suppressed": len((r.get("verification") or {}).get("suppressed_claims") or []),
                "verification_ms": (r.get("timing_ms") or {}).get("verification_ms"),
                "cost_usd": r.get("estimated_cost_usd"),
            }
            for r in rows
        },
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(f"DONE n={len(rows)} spent={spent:.4f} out={OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
