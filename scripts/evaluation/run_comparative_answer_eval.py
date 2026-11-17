"""Phase 9D.1 comparative answer evaluation (internal / external-only / auto).

Examples:
  python scripts/evaluation/run_comparative_answer_eval.py --dry-run
  python scripts/evaluation/run_comparative_answer_eval.py --pilot --max-cost-usd 2
  python scripts/evaluation/run_comparative_answer_eval.py --full --max-cost-usd 10
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from lifepilot.agent import AgentRequest, AgentStatus, run_agent  # noqa: E402
from lifepilot.agent.config import get_agent_config  # noqa: E402
from lifepilot.agent.orchestration import (  # noqa: E402
    get_evidence_orchestrator,
    reset_evidence_orchestrator_override,
    set_evidence_orchestrator_override,
)
from lifepilot.agent.providers import (  # noqa: E402
    anthropic_api_key_present,
    get_llm_provider,
    reset_llm_provider_cache,
)
from lifepilot.evaluation.accounting import estimate_usage_cost_usd  # noqa: E402
from lifepilot.evaluation.comparative import (  # noqa: E402
    MODES,
    ExternalOnlyEvidenceOrchestrator,
    classify_automated_assessment,
    retrieval_options_for_mode,
)

import importlib.util  # noqa: E402


def _load_grounded_eval_helpers():
    path = ROOT / "scripts" / "evaluation" / "run_grounded_qa_eval.py"
    spec = importlib.util.spec_from_file_location("run_grounded_qa_eval", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load run_grounded_qa_eval helpers")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.score_case, mod.serialize_response


score_case, serialize_response = _load_grounded_eval_helpers()

ORIGINAL_BENCH = ROOT / "config" / "eval" / "agent_qa_benchmark_v1.json"
SUPPLEMENTAL = ROOT / "config" / "eval" / "agent_qa_supplemental_9d1.json"
REFERENCE = ROOT / "config" / "eval" / "reference_evidence_packets_9d1.json"
OUT_DIR = ROOT / "data" / "runs" / "comparative_answer_eval"
RESULTS_NAME = "results.jsonl"
SUMMARY_NAME = "summary.json"
REPORT_PATH = ROOT / "data" / "runs" / "phase_9d1_comparative_answer_evaluation.md"
WORKSHEET_PATH = OUT_DIR / "human_review_worksheet.json"

PILOT_CASE_IDS = [
    "E01",
    "F01",
    "F07",
    "E15",
    "S05",
    "X01",
    "X04",
    "X05",
    "X07",
    "X14",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_cases(
    *,
    include_original: bool,
    include_supplemental: bool,
    include_held_out: bool,
    case_ids: list[str] | None,
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    if include_original:
        bench = _load_json(ORIGINAL_BENCH)
        assert len(bench["cases"]) == 50
        for c in bench["cases"]:
            cases.append({**c, "benchmark_source": "original_v1", "held_out": False})
    if include_supplemental:
        supp = _load_json(SUPPLEMENTAL)
        for c in supp["cases"]:
            if c.get("held_out") and not include_held_out:
                continue
            cases.append({**c, "benchmark_source": "supplemental_9d1"})
    if case_ids:
        wanted = set(case_ids)
        cases = [c for c in cases if c["id"] in wanted]
    # Stable unique by id (first wins)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in cases:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        out.append(c)
    return out


def _result_key(case_id: str, mode: str) -> str:
    return f"{case_id}::{mode}"


def _load_results(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows[_result_key(row["question_id"], row["mode"])] = row
    return rows


def _append_result(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _estimate_cost(usage: dict[str, Any] | None, cfg) -> float:
    if not usage:
        return 0.0
    if usage.get("estimated_cost_usd") is not None:
        return float(usage["estimated_cost_usd"] or 0.0)
    in_rate = float(cfg.llm_input_cost_per_mtok or 2.0)
    out_rate = float(cfg.llm_output_cost_per_mtok or 10.0)
    return estimate_usage_cost_usd(
        usage,
        input_cost_per_mtok=in_rate,
        output_cost_per_mtok=out_rate,
    )


def run_case_mode(case: dict[str, Any], mode: str) -> dict[str, Any]:
    cfg = get_agent_config()
    started = time.perf_counter()
    retrieval = retrieval_options_for_mode(mode)
    override = None
    if mode == "external_only":
        override = ExternalOnlyEvidenceOrchestrator(get_evidence_orchestrator(cfg))
        set_evidence_orchestrator_override(override)
    try:
        response = run_agent(
            AgentRequest(
                question=case["question"],
                jurisdiction=case["jurisdiction"],
                pathway_id=case.get("pathway_id"),
                retrieval=retrieval,
                conversation_id=f"cmp_{case['id']}_{mode}",
            )
        )
    finally:
        if override is not None:
            reset_evidence_orchestrator_override()

    wall_ms = round((time.perf_counter() - started) * 1000.0, 2)
    payload = serialize_response(response)
    llm_ran = response.timing.llm_ms is not None and response.timing.llm_ms > 0
    if response.status == AgentStatus.INSUFFICIENT_EVIDENCE and not llm_ran:
        exec_status = "insufficient_evidence_no_llm"
        paid = False
    elif response.status == AgentStatus.EXECUTION_FAILURE:
        exec_status = "execution_failure"
        paid = llm_ran
    elif llm_ran:
        exec_status = "success_paid"
        paid = True
    else:
        exec_status = "completed_no_llm"
        paid = False

    evaluation = score_case(case, payload, None)
    usage = payload.get("usage") or {}
    cost = _estimate_cost(usage, cfg)
    auto = classify_automated_assessment(case=case, row={**payload, **{"claims": payload.get("claims"), "evidence": payload.get("evidence"), "agent_status": payload.get("status")}})

    evidence_types = sorted(
        {
            (e.get("source_type") or "")
            for e in (payload.get("evidence") or [])
            if e.get("source_type")
        }
    )
    return {
        "question_id": case["id"],
        "mode": mode,
        "benchmark_source": case.get("benchmark_source"),
        "held_out": bool(case.get("held_out")),
        "category": case.get("category") or case.get("risk_category"),
        "jurisdiction": case.get("jurisdiction"),
        "pathway_id": case.get("pathway_id"),
        "question": case.get("question"),
        "as_of_date": case.get("as_of_date"),
        "expected_behavior": case.get("expected_behavior") or case.get("expected_abstention"),
        "personal_eligibility_prohibited": case.get("personal_eligibility_prohibited"),
        "evaluated_at": _utc_now(),
        "model": (usage.get("model") or cfg.llm_model or None),
        "provider": (usage.get("provider") or cfg.llm_provider or None),
        "retrieval": retrieval,
        "retrieval_decision": payload.get("retrieval_decision"),
        "execution_status": exec_status,
        "paid_call": paid,
        "agent_status": payload.get("status"),
        "answer": payload.get("answer"),
        "claims": payload.get("claims"),
        "pre_verification_claims": payload.get("pre_verification_claims"),
        "verification": payload.get("verification"),
        "warnings": payload.get("warnings"),
        "errors": payload.get("errors"),
        "missing_information": payload.get("missing_information"),
        "evidence_source_types": evidence_types,
        "evidence_count": len(payload.get("evidence") or []),
        "evidence": [
            {
                "evidence_id": e.get("evidence_id"),
                "source_type": e.get("source_type"),
                "verification_state": e.get("verification_state"),
                "jurisdiction": e.get("jurisdiction"),
                "source_url": e.get("source_url"),
                "passage_text": (e.get("passage_text") or "")[:500],
            }
            for e in (payload.get("evidence") or [])
        ],
        "timing_ms": {
            "wall_total": wall_ms,
            "total": (payload.get("timing") or {}).get("total_ms"),
            "evidence": (payload.get("timing") or {}).get("evidence_ms"),
            "internal_retrieval": (payload.get("timing") or {}).get(
                "internal_retrieval_ms"
            ),
            "external_retrieval": (payload.get("timing") or {}).get(
                "external_retrieval_ms"
            ),
            "llm": (payload.get("timing") or {}).get("llm_ms"),
            "grounding": (payload.get("timing") or {}).get("grounding_ms"),
            "verification": (payload.get("timing") or {}).get("verification_ms"),
        },
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "estimated_cost_usd": cost,
        },
        "evaluation": evaluation,
        "automated_assessment": auto,
        "reference_verification_status": "not_human_verified",
    }


def _is_complete(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return row.get("execution_status") in {
        "success_paid",
        "insufficient_evidence_no_llm",
        "completed_no_llm",
        "invalid_request",
    }


def _summarize(rows: list[dict[str, Any]], budgets: dict[str, Any]) -> dict[str, Any]:
    by_mode: dict[str, list[dict[str, Any]]] = {m: [] for m in MODES}
    for r in rows:
        by_mode.setdefault(r["mode"], []).append(r)

    def mode_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
        paid = [i for i in items if i.get("paid_call")]
        walls = [
            float((i.get("timing_ms") or {}).get("wall_total") or 0)
            for i in items
            if (i.get("timing_ms") or {}).get("wall_total") is not None
        ]
        costs = [
            float((i.get("usage") or {}).get("estimated_cost_usd") or 0) for i in items
        ]
        decisions = {}
        for i in items:
            d = i.get("retrieval_decision") or "unknown"
            decisions[d] = decisions.get(d, 0) + 1
        success = sum(1 for i in items if i.get("execution_status") == "success_paid")
        insuff = sum(
            1
            for i in items
            if i.get("execution_status") == "insufficient_evidence_no_llm"
        )
        return {
            "n": len(items),
            "success_paid": success,
            "insufficient_evidence_no_llm": insuff,
            "paid_calls": len(paid),
            "estimated_cost_usd": round(sum(costs), 6),
            "latency_ms_median": (
                statistics.median(walls) if walls else None
            ),
            "latency_ms_p95": (
                sorted(walls)[max(0, int(round(0.95 * (len(walls) - 1))))]
                if walls
                else None
            ),
            "retrieval_decisions": decisions,
            "external_invocation_rate": (
                round(
                    sum(
                        1
                        for i in items
                        if (i.get("timing_ms") or {}).get("external_retrieval")
                        and float((i.get("timing_ms") or {}).get("external_retrieval") or 0)
                        > 0
                    )
                    / max(1, len(items)),
                    3,
                )
            ),
        }

    paired: list[dict[str, Any]] = []
    by_qid: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        by_qid.setdefault(r["question_id"], {})[r["mode"]] = r
    for qid, modes in sorted(by_qid.items()):
        paired.append(
            {
                "question_id": qid,
                "modes_present": sorted(modes.keys()),
                "statuses": {m: modes[m].get("execution_status") for m in modes},
                "retrieval_decisions": {
                    m: modes[m].get("retrieval_decision") for m in modes
                },
                "evidence_counts": {
                    m: modes[m].get("evidence_count") for m in modes
                },
                "automated_factual": {
                    m: (modes[m].get("automated_assessment") or {}).get(
                        "factual_legal_correctness_automated"
                    )
                    for m in modes
                },
                "costs_usd": {
                    m: (modes[m].get("usage") or {}).get("estimated_cost_usd")
                    for m in modes
                },
            }
        )

    return {
        "generated_at": _utc_now(),
        "budgets": budgets,
        "totals": {
            "rows": len(rows),
            "paid_calls": sum(1 for r in rows if r.get("paid_call")),
            "estimated_cost_usd": round(
                sum(float((r.get("usage") or {}).get("estimated_cost_usd") or 0) for r in rows),
                6,
            ),
        },
        "by_mode": {m: mode_stats(by_mode.get(m, [])) for m in MODES},
        "paired_comparisons": paired,
        "limitations": [
            "Automated factual labels are not independent legal verification.",
            "Reference packets mostly require_human_review.",
            "External-only is diagnostic and not a production mode.",
        ],
    }


def _write_report(summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Phase 9D.1 — Comparative Answer Accuracy & Hybrid Retrieval Evaluation",
        "",
        f"**Generated:** {summary.get('generated_at')}",
        "",
        "## 1. Methodology",
        "",
        "- Modes: `internal_only` (`external_mode=off`), `external_only` (eval-only adapter), `auto_hybrid` (`external_mode=auto`).",
        "- Same questions/model settings across modes; only retrieval strategy differs.",
        "- Original benchmark `agent_qa_benchmark_v1` left unchanged.",
        "- Supplemental cases: `agent_qa_supplemental_9d1.json` (held-out flagged).",
        "- Independent legal accuracy is **not** claimed from automated labels alone.",
        "",
        "## 2. Execution configuration",
        "",
        "```json",
        json.dumps(summary.get("budgets") or {}, indent=2),
        "```",
        "",
        "## 3. Totals",
        "",
        f"- Rows: {summary['totals']['rows']}",
        f"- Paid LLM calls: {summary['totals']['paid_calls']}",
        f"- Estimated cost USD: {summary['totals']['estimated_cost_usd']}",
        "",
        "## 4. Results by mode",
        "",
    ]
    for mode, stats in (summary.get("by_mode") or {}).items():
        lines.append(f"### {mode}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(stats, indent=2))
        lines.append("```")
        lines.append("")

    lines.extend(
        [
            "## 5. Question-level paired comparisons",
            "",
            "See `data/runs/comparative_answer_eval/summary.json` → `paired_comparisons`.",
            "",
            "## 6. Factual correctness note",
            "",
            "Automated assessment distinguishes heuristic safety/citation issues from",
            "`unverifiable_from_available_reference_evidence`. Human review worksheet:",
            f"`{WORKSHEET_PATH.as_posix()}`.",
            "",
            "## 7. Hybrid retrieval observations",
            "",
        ]
    )
    auto_rows = [r for r in rows if r.get("mode") == "auto_hybrid"]
    if auto_rows:
        decisions = {}
        for r in auto_rows:
            d = r.get("retrieval_decision") or "unknown"
            decisions[d] = decisions.get(d, 0) + 1
        lines.append(f"- Auto retrieval decisions: `{decisions}`")
        lines.append(
            f"- External invocation rate: "
            f"{(summary.get('by_mode') or {}).get('auto_hybrid', {}).get('external_invocation_rate')}"
        )
    else:
        lines.append("- No auto_hybrid rows completed.")
    lines.extend(
        [
            "",
            "## 8. Recommendation",
            "",
            "- Keep `external_mode=auto` as production default **if** pilot/full results show",
            "  no safety regressions versus internal-only and selective connector use.",
            "- Do not treat this report as production-ready legal certification.",
            "",
            "## 9. Incomplete / blockers",
            "",
            "- Any unpaid/incomplete runs are listed in results JSONL by missing mode pairs.",
            "- Reference packets remain largely `requires_human_review`.",
            "",
        ]
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    auto_path = OUT_DIR / "report_auto.md"
    auto_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Preserve a manually finalized primary report (Phase 9D.1 deliverable).
    if REPORT_PATH.exists() and REPORT_PATH.stat().st_size >= 4000:
        return
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_worksheet(rows: list[dict[str, Any]], reference: dict[str, Any]) -> None:
    items = []
    for r in rows:
        if r.get("mode") != "auto_hybrid":
            continue
        items.append(
            {
                "question_id": r["question_id"],
                "jurisdiction": r.get("jurisdiction"),
                "pathway_id": r.get("pathway_id"),
                "question": r.get("question"),
                "answer_preview": (r.get("answer") or "")[:400],
                "automated_assessment": r.get("automated_assessment"),
                "human_verdict": "not_yet_verified",
                "reference_packets": [
                    p
                    for p in (reference.get("packets") or [])
                    if r["question_id"] in (p.get("case_ids") or [])
                ],
            }
        )
    WORKSHEET_PATH.parent.mkdir(parents=True, exist_ok=True)
    WORKSHEET_PATH.write_text(
        json.dumps(
            {
                "generated_at": _utc_now(),
                "note": "Human verdicts must be filled by authorized reviewers.",
                "items": items,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 9D.1 comparative evaluation")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--include-held-out", action="store_true")
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--modes", nargs="+", default=list(MODES), choices=list(MODES))
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    cfg = get_agent_config()
    max_cost = float(
        args.max_cost_usd
        if args.max_cost_usd is not None
        else os.getenv("LIFEPILOT_EVAL_MAX_COST_USD", "0")
    )
    max_calls = int(
        args.max_calls
        if args.max_calls is not None
        else os.getenv("LIFEPILOT_EVAL_MAX_CALLS", "0")
    )

    if args.pilot:
        case_ids = list(args.case_id or PILOT_CASE_IDS)
        include_original = True
        include_supplemental = True
        include_held_out = False
        if args.max_cost_usd is None:
            max_cost = 2.0
        if args.max_calls is None:
            max_calls = 40
    elif args.full:
        case_ids = list(args.case_id) if args.case_id else None
        include_original = True
        include_supplemental = True
        include_held_out = bool(args.include_held_out)
        if args.max_cost_usd is None:
            max_cost = 10.0
        if args.max_calls is None:
            max_calls = 250
    else:
        case_ids = list(args.case_id) if args.case_id else PILOT_CASE_IDS
        include_original = True
        include_supplemental = True
        include_held_out = False

    cases = _load_cases(
        include_original=include_original,
        include_supplemental=include_supplemental,
        include_held_out=include_held_out,
        case_ids=case_ids,
    )
    jobs = [(c, m) for c in cases for m in args.modes]

    if args.dry_run:
        print(f"cases={len(cases)} jobs={len(jobs)} modes={args.modes}")
        print(f"max_cost_usd={max_cost} max_calls={max_calls}")
        print(f"key_present={anthropic_api_key_present()} provider={get_llm_provider().provider_id}")
        for c in cases:
            print(f"  - {c['id']} {c.get('jurisdiction')} held_out={c.get('held_out')}")
        return 0

    if cfg.llm_input_cost_per_mtok <= 0 or cfg.llm_output_cost_per_mtok <= 0:
        raise SystemExit("Set LIFEPILOT_LLM_INPUT/OUTPUT_COST_PER_MTOK before paid runs.")
    if max_cost <= 0 or max_calls <= 0:
        raise SystemExit("max-cost-usd and max-calls must be > 0 for paid runs.")
    if not anthropic_api_key_present() or get_llm_provider().provider_id == "disabled":
        raise SystemExit(
            "Anthropic not configured. Offline prep is complete; cannot run paid comparison."
        )

    reset_llm_provider_cache()
    out_dir = args.out_dir
    results_path = out_dir / RESULTS_NAME
    existing = _load_results(results_path)
    spent = sum(
        float((r.get("usage") or {}).get("estimated_cost_usd") or 0)
        for r in existing.values()
        if r.get("paid_call")
    )
    live_calls = sum(1 for r in existing.values() if r.get("paid_call"))

    budgets = {
        "max_cost_usd": max_cost,
        "max_calls": max_calls,
        "model": cfg.llm_model,
        "provider": cfg.llm_provider,
        "input_cost_per_mtok": cfg.llm_input_cost_per_mtok,
        "output_cost_per_mtok": cfg.llm_output_cost_per_mtok,
        "stage": "pilot" if args.pilot else ("full" if args.full else "custom"),
    }

    stopped = None
    for case, mode in jobs:
        key = _result_key(case["id"], mode)
        if _is_complete(existing.get(key)):
            print(f"SKIP {key} (resume)", flush=True)
            continue
        if live_calls >= max_calls:
            stopped = "max_calls"
            break
        if spent >= max_cost:
            stopped = "max_cost"
            break
        print(f"RUN {key} spent={spent:.4f}", flush=True)
        row = run_case_mode(case, mode)
        _append_result(results_path, row)
        existing[key] = row
        if row.get("paid_call"):
            live_calls += 1
            spent += float((row.get("usage") or {}).get("estimated_cost_usd") or 0)
        if spent > max_cost:
            stopped = "max_cost_after_call"
            break

    rows = list(existing.values())
    summary = _summarize(rows, {**budgets, "stopped_reason": stopped, "spent_usd": spent})
    (out_dir / SUMMARY_NAME).write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    reference = _load_json(REFERENCE)
    _write_worksheet(rows, reference)
    _write_report(summary, rows)
    print(
        f"DONE rows={len(rows)} paid={live_calls} spent={spent:.4f} stopped={stopped} "
        f"report={REPORT_PATH}"
    )
    return 0 if stopped is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
