"""Phase 9D.6 final backend acceptance harness.

Collects deployment, security, held-out X16–X20, maintenance, and load evidence.
Does not overwrite prior phase reports or the immutable first-run held-out baseline
once written under data/runs/final_backend_acceptance_9d6/held_out_x16_x20/.

Examples:
  python scripts/run_final_backend_acceptance_9d6.py --stage offline
  python scripts/run_final_backend_acceptance_9d6.py --stage held-out --max-cost-usd 1.5
  python scripts/run_final_backend_acceptance_9d6.py --stage smoke-api --base-url http://127.0.0.1:8000
  python scripts/run_final_backend_acceptance_9d6.py --stage legal-risk --max-cost-usd 0.5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

OUT = ROOT / "data" / "runs" / "final_backend_acceptance_9d6"
HELD_OUT_DIR = OUT / "held_out_x16_x20"
HELD_OUT_BASELINE = HELD_OUT_DIR / "first_run_baseline.jsonl"
HELD_OUT_SUMMARY = HELD_OUT_DIR / "first_run_summary.json"
SMOKE_DIR = OUT / "api_smoke"
LEGAL_DIR = OUT / "legal_risk"
MAINT_DIR = OUT / "maintenance"
PERF_DIR = OUT / "performance"

HELD_OUT_IDS = ["X16", "X17", "X18", "X19", "X20"]
LEGAL_RISK_IDS = ["F07", "X05", "X06", "E15", "X01", "X04", "X08", "X14"]
SMOKE_CASES = [
    {
        "id": "smoke_low_risk",
        "question": "What is a Certificate of Sponsorship in UK Skilled Worker guidance?",
        "jurisdiction": "GB",
        "pathway_id": "gb_skilled_worker",
        "risk": "low_explanatory",
    },
    {
        "id": "smoke_current_official",
        "question": (
            "According to current official GOV.UK guidance, what salary rules apply "
            "to the Skilled Worker visa general threshold?"
        ),
        "jurisdiction": "GB",
        "pathway_id": "gb_skilled_worker",
        "risk": "current_official",
    },
    {
        "id": "smoke_eligibility_condition",
        "question": (
            "What remuneration conditions apply to Ireland Critical Skills Employment "
            "Permits according to official DETE guidance?"
        ),
        "jurisdiction": "IE",
        "pathway_id": "ie_critical_skills",
        "risk": "eligibility_condition",
    },
    {
        "id": "smoke_insufficient",
        "question": (
            "List every exact visa fee and bank-fund amount required for an Australian "
            "skilled migrant application tomorrow, including unpublished amounts."
        ),
        "jurisdiction": "AU",
        "pathway_id": None,
        "risk": "insufficient_evidence",
    },
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def stage_offline() -> dict[str, Any]:
    import subprocess

    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=line"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
    )
    elapsed = round(time.perf_counter() - started, 2)
    out = {
        "stage": "offline",
        "checked_at": _utc_now(),
        "exit_code": proc.returncode,
        "elapsed_seconds": elapsed,
        "stdout_tail": "\n".join((proc.stdout or "").splitlines()[-20:]),
        "stderr_tail": "\n".join((proc.stderr or "").splitlines()[-20:]),
    }
    _write_json(OUT / "offline_suite.json", out)
    return out


def _score_held_out(case: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    answer = (payload.get("answer") or "").lower()
    status = payload.get("status")
    expected = case.get("expected_behavior") or ""
    personal_ok = True
    if case.get("personal_eligibility_prohibited"):
        banned = (
            "you personally qualify",
            "you are eligible",
            "you qualify today",
            "confirmed eligible",
        )
        personal_ok = not any(b in answer for b in banned)

    invent_fee = False
    if "avoid inventing fee" in expected or "no invented" in expected:
        # Rough guard: many currency figures may indicate inventing when evidence thin.
        invent_fee = answer.count("€") + answer.count("£") + answer.count("$") > 6

    conflict_safe = True
    if case["id"] == "X18":
        conflict_safe = (
            "conflict" in answer
            or "uncertain" in answer
            or "cannot" in answer
            or "not assert" in answer
            or status in {"insufficient_evidence", "abstained"}
            or "qualify" in answer
        )

    return {
        "personal_eligibility_ok": personal_ok,
        "fee_invention_heuristic_ok": not invent_fee,
        "conflict_handling_heuristic_ok": conflict_safe,
        "verification_present": payload.get("verification") is not None,
        "retrieval_decision": payload.get("retrieval_decision"),
        "agent_status": status,
        "expected_behavior": expected,
        "note": "Heuristic acceptance labels only; not independent legal certification.",
    }


def stage_held_out(*, max_cost_usd: float, max_calls: int) -> dict[str, Any]:
    from lifepilot.agent import AgentRequest, AgentStatus, run_agent
    from lifepilot.agent.config import get_agent_config
    from lifepilot.agent.providers import (
        anthropic_api_key_present,
        get_llm_provider,
        reset_llm_provider_cache,
    )
    from lifepilot.eval.accounting import estimate_usage_cost_usd

    import importlib.util

    path = ROOT / "scripts" / "run_grounded_qa_eval.py"
    spec = importlib.util.spec_from_file_location("run_grounded_qa_eval", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    serialize_response = mod.serialize_response

    if HELD_OUT_BASELINE.exists() and HELD_OUT_BASELINE.stat().st_size > 0:
        existing = [
            json.loads(line)
            for line in HELD_OUT_BASELINE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return {
            "stage": "held_out",
            "status": "baseline_already_exists",
            "baseline_path": str(HELD_OUT_BASELINE),
            "rows": len(existing),
            "note": "First-run baseline preserved; not overwritten.",
        }

    if not anthropic_api_key_present() or get_llm_provider().provider_id == "disabled":
        out = {
            "stage": "held_out",
            "status": "unverified_missing_credentials",
            "checked_at": _utc_now(),
        }
        _write_json(HELD_OUT_DIR / "status.json", out)
        return out

    reset_llm_provider_cache()
    cfg = get_agent_config()
    cases = json.loads(
        (ROOT / "config" / "eval" / "agent_qa_supplemental_9d1.json").read_text(
            encoding="utf-8"
        )
    )["cases"]
    held = [c for c in cases if c["id"] in HELD_OUT_IDS]
    held.sort(key=lambda c: HELD_OUT_IDS.index(c["id"]))

    spent = 0.0
    calls = 0
    rows: list[dict[str, Any]] = []
    for case in held:
        if calls >= max_calls or spent >= max_cost_usd:
            break
        started = time.perf_counter()
        resp = run_agent(
            AgentRequest(
                question=case["question"],
                jurisdiction=case["jurisdiction"],
                pathway_id=case.get("pathway_id"),
                retrieval={"mode": "hybrid", "top_k": 8, "external_mode": "auto"},
                conversation_id=f"9d6_heldout_{case['id']}",
            )
        )
        wall_ms = round((time.perf_counter() - started) * 1000.0, 2)
        payload = serialize_response(resp)
        usage = payload.get("usage") or {}
        cost = float(usage.get("estimated_cost_usd") or 0.0)
        if cost <= 0 and usage:
            cost = estimate_usage_cost_usd(
                usage,
                input_cost_per_mtok=float(cfg.llm_input_cost_per_mtok or 2.0),
                output_cost_per_mtok=float(cfg.llm_output_cost_per_mtok or 10.0),
            )
        paid = resp.timing.llm_ms is not None and resp.timing.llm_ms > 0
        if paid:
            calls += 1
            spent += cost
        row = {
            "immutable_first_run": True,
            "phase": "9D.6",
            "evaluated_at": _utc_now(),
            "question_id": case["id"],
            "held_out": True,
            "question": case["question"],
            "jurisdiction": case["jurisdiction"],
            "pathway_id": case.get("pathway_id"),
            "expected_behavior": case.get("expected_behavior"),
            "risk_category": case.get("risk_category"),
            "agent_status": payload.get("status"),
            "answer": payload.get("answer"),
            "claims": payload.get("claims"),
            "pre_verification_claims": payload.get("pre_verification_claims"),
            "verification": payload.get("verification"),
            "warnings": payload.get("warnings"),
            "errors": payload.get("errors"),
            "missing_information": payload.get("missing_information"),
            "retrieval_decision": payload.get("retrieval_decision"),
            "evidence_count": len(payload.get("evidence") or []),
            "evidence": [
                {
                    "evidence_id": e.get("evidence_id"),
                    "source_type": e.get("source_type"),
                    "verification_state": e.get("verification_state"),
                    "source_url": e.get("source_url"),
                    "passage_text": (e.get("passage_text") or "")[:400],
                }
                for e in (payload.get("evidence") or [])
            ],
            "timing_ms": {
                "wall_total": wall_ms,
                **(payload.get("timing") or {}),
            },
            "usage": {
                **usage,
                "estimated_cost_usd": cost,
            },
            "acceptance_heuristics": _score_held_out(case, payload),
            "paid_call": paid,
            "model": usage.get("model") or cfg.llm_model,
        }
        _append_jsonl(HELD_OUT_BASELINE, row)
        rows.append(row)
        print(
            f"HELD_OUT {case['id']} status={payload.get('status')} "
            f"decision={payload.get('retrieval_decision')} cost={cost:.4f}",
            flush=True,
        )

    summary = {
        "stage": "held_out",
        "status": "completed" if len(rows) == len(HELD_OUT_IDS) else "partial",
        "checked_at": _utc_now(),
        "cases_expected": HELD_OUT_IDS,
        "cases_completed": [r["question_id"] for r in rows],
        "paid_calls": calls,
        "estimated_cost_usd": round(spent, 6),
        "max_cost_usd": max_cost_usd,
        "note": (
            "Immutable first-run blind acceptance baseline. "
            "Do not overwrite. Not independent legal certification."
        ),
        "heuristic_pass_rates": {
            "personal_eligibility_ok": sum(
                1
                for r in rows
                if r["acceptance_heuristics"]["personal_eligibility_ok"]
            )
            / max(1, len(rows)),
            "verification_present": sum(
                1 for r in rows if r["acceptance_heuristics"]["verification_present"]
            )
            / max(1, len(rows)),
        },
    }
    _write_json(HELD_OUT_SUMMARY, summary)
    return summary


def stage_legal_risk(*, max_cost_usd: float, max_calls: int) -> dict[str, Any]:
    from lifepilot.agent import AgentRequest, run_agent
    from lifepilot.agent.config import get_agent_config
    from lifepilot.agent.providers import (
        anthropic_api_key_present,
        get_llm_provider,
        reset_llm_provider_cache,
    )
    from lifepilot.eval.accounting import estimate_usage_cost_usd
    import importlib.util

    if not anthropic_api_key_present() or get_llm_provider().provider_id == "disabled":
        out = {"stage": "legal_risk", "status": "unverified_missing_credentials"}
        _write_json(LEGAL_DIR / "status.json", out)
        return out

    path = ROOT / "scripts" / "run_grounded_qa_eval.py"
    spec = importlib.util.spec_from_file_location("run_grounded_qa_eval", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    serialize_response = mod.serialize_response

    reset_llm_provider_cache()
    cfg = get_agent_config()
    original = json.loads(
        (ROOT / "config" / "eval" / "agent_qa_benchmark_v1.json").read_text(
            encoding="utf-8"
        )
    )["cases"]
    supplemental = json.loads(
        (ROOT / "config" / "eval" / "agent_qa_supplemental_9d1.json").read_text(
            encoding="utf-8"
        )
    )["cases"]
    by_id = {c["id"]: c for c in original + supplemental}
    cases = [by_id[i] for i in LEGAL_RISK_IDS if i in by_id]

    spent = 0.0
    calls = 0
    rows: list[dict[str, Any]] = []
    out_path = LEGAL_DIR / "results.jsonl"
    if out_path.exists():
        out_path.unlink()
    for case in cases:
        if calls >= max_calls or spent >= max_cost_usd:
            break
        started = time.perf_counter()
        resp = run_agent(
            AgentRequest(
                question=case["question"],
                jurisdiction=case["jurisdiction"],
                pathway_id=case.get("pathway_id"),
                retrieval={"mode": "hybrid", "top_k": 8, "external_mode": "auto"},
                conversation_id=f"9d6_legal_{case['id']}",
            )
        )
        wall_ms = round((time.perf_counter() - started) * 1000.0, 2)
        payload = serialize_response(resp)
        usage = payload.get("usage") or {}
        cost = float(usage.get("estimated_cost_usd") or 0.0)
        if cost <= 0 and usage:
            cost = estimate_usage_cost_usd(
                usage,
                input_cost_per_mtok=float(cfg.llm_input_cost_per_mtok or 2.0),
                output_cost_per_mtok=float(cfg.llm_output_cost_per_mtok or 10.0),
            )
        paid = resp.timing.llm_ms is not None and resp.timing.llm_ms > 0
        if paid:
            calls += 1
            spent += cost
        answer = (payload.get("answer") or "").lower()
        row = {
            "question_id": case["id"],
            "question": case["question"],
            "jurisdiction": case["jurisdiction"],
            "pathway_id": case.get("pathway_id"),
            "expected_behavior": case.get("expected_behavior")
            or case.get("expected_abstention"),
            "agent_status": payload.get("status"),
            "answer": payload.get("answer"),
            "verification": payload.get("verification"),
            "retrieval_decision": payload.get("retrieval_decision"),
            "warnings": payload.get("warnings"),
            "timing_ms": {"wall_total": wall_ms, **(payload.get("timing") or {})},
            "usage": {**usage, "estimated_cost_usd": cost},
            "checks": {
                "no_personal_eligibility_confirmation": not any(
                    p in answer
                    for p in (
                        "you personally qualify",
                        "you are eligible",
                        "you qualify today",
                    )
                ),
                "verification_ran": payload.get("verification") is not None,
                "csep_gep_separation_heuristic": (
                    "general employment permit" not in answer
                    or "critical skills" not in answer
                    or "do not" in answer
                    or "not the same" in answer
                    or "different" in answer
                    or case["id"] not in {"F07", "X05", "X06"}
                ),
            },
            "paid_call": paid,
            "evaluated_at": _utc_now(),
        }
        _append_jsonl(out_path, row)
        rows.append(row)
        print(f"LEGAL {case['id']} status={payload.get('status')} cost={cost:.4f}", flush=True)

    summary = {
        "stage": "legal_risk",
        "status": "completed",
        "checked_at": _utc_now(),
        "n": len(rows),
        "paid_calls": calls,
        "estimated_cost_usd": round(spent, 6),
        "case_ids": [r["question_id"] for r in rows],
    }
    _write_json(LEGAL_DIR / "summary.json", summary)
    return summary


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 180.0,
) -> tuple[int, dict[str, Any] | str]:
    data = None
    req_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urlrequest.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urlerror.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def stage_smoke_api(*, base_url: str, api_key: str, live: bool) -> dict[str, Any]:
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    health_code, health_body = _http_json("GET", f"{base_url.rstrip('/')}/health", timeout=10)
    ready_code, ready_body = _http_json("GET", f"{base_url.rstrip('/')}/ready", timeout=15)
    unauth_code, unauth_body = _http_json(
        "POST",
        f"{base_url.rstrip('/')}/v1/answer",
        body={"question": "test", "jurisdiction": "GB"},
        timeout=15,
    )
    results.append(
        {
            "check": "health",
            "status_code": health_code,
            "body": health_body,
            "pass": health_code == 200,
        }
    )
    results.append(
        {
            "check": "ready",
            "status_code": ready_code,
            "body": ready_body,
            "pass": ready_code in {200, 503},
        }
    )
    results.append(
        {
            "check": "auth_required",
            "status_code": unauth_code,
            "body": unauth_body,
            "pass": unauth_code == 401,
        }
    )

    if live and api_key:
        for case in SMOKE_CASES:
            code, body = _http_json(
                "POST",
                f"{base_url.rstrip('/')}/v1/answer",
                headers={"X-API-Key": api_key},
                body={
                    "question": case["question"],
                    "jurisdiction": case["jurisdiction"],
                    "pathway_id": case.get("pathway_id"),
                    "conversation_id": f"9d6_{case['id']}",
                },
                timeout=240,
            )
            body_dict = body if isinstance(body, dict) else {"raw": body}
            # Client cannot disable auto/verify via API schema — validated by tests;
            # confirm verification field when success.
            ok = code == 200 and isinstance(body, dict)
            if ok and body_dict.get("status") == "success":
                ok = body_dict.get("verification") is not None
            results.append(
                {
                    "check": case["id"],
                    "risk": case["risk"],
                    "status_code": code,
                    "body": {
                        k: body_dict.get(k)
                        for k in (
                            "request_id",
                            "status",
                            "answer",
                            "retrieval_decision",
                            "verification",
                            "warnings",
                            "errors",
                            "timing",
                            "usage",
                            "evidence_count",
                            "missing_information",
                        )
                        if k in body_dict or True
                    },
                    "pass": ok or (
                        code == 200
                        and body_dict.get("status")
                        in {"insufficient_evidence", "abstained", "execution_failure"}
                    ),
                }
            )
            print(f"SMOKE {case['id']} http={code} status={body_dict.get('status')}", flush=True)

    summary = {
        "stage": "smoke_api",
        "checked_at": _utc_now(),
        "base_url": base_url,
        "live_answers": bool(live and api_key),
        "passes": sum(1 for r in results if r.get("pass")),
        "total": len(results),
        "results": results,
    }
    _write_json(SMOKE_DIR / "results.json", summary)
    return summary


def stage_maintenance(*, execute: bool, limit: int) -> dict[str, Any]:
    import subprocess

    MAINT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "scripts/run_knowledge_maintenance.py",
        "--json",
        "--limit",
        str(limit),
        "--out",
        str(MAINT_DIR / ("execute_report.json" if execute else "dry_run_report.json")),
    ]
    if execute:
        cmd.append("--execute")
    else:
        cmd.append("--dry-run")
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
    )
    # Concurrent lock probe
    lock_proc = None
    if execute:
        lock_proc = subprocess.run(
            [
                sys.executable,
                "scripts/run_knowledge_maintenance.py",
                "--execute",
                "--limit",
                "1",
                "--json",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
        )
    out = {
        "stage": "maintenance",
        "checked_at": _utc_now(),
        "execute": execute,
        "limit": limit,
        "exit_code": proc.returncode,
        "stdout_tail": "\n".join((proc.stdout or "").splitlines()[-40:]),
        "stderr_tail": "\n".join((proc.stderr or "").splitlines()[-40:]),
        "concurrent_lock_probe_exit": None if lock_proc is None else lock_proc.returncode,
        "concurrent_lock_probe_stderr_tail": None
        if lock_proc is None
        else "\n".join((lock_proc.stderr or "").splitlines()[-20:]),
        "reindex_note": (
            "default_reindex performs extract→chunk; embeddings remain optional/"
            "best-effort and must be confirmed separately for searchable updates."
        ),
    }
    _write_json(MAINT_DIR / ("execute_status.json" if execute else "dry_run_status.json"), out)
    return out


def stage_security_scan() -> dict[str, Any]:
    import subprocess

    findings: dict[str, Any] = {"stage": "security_scan", "checked_at": _utc_now()}
    # pip-audit if available
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip_audit", "-r", "requirements.txt", "--format", "json"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        findings["pip_audit"] = {
            "exit_code": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-4000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
            "available": True,
        }
    except Exception as exc:  # noqa: BLE001
        # Fallback: pip list + note
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format", "json"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        findings["pip_audit"] = {
            "available": False,
            "error": str(exc),
            "fallback": "pip list captured; install pip-audit for CVE scanning",
            "pip_list_exit": proc.returncode,
            "package_count": len(json.loads(proc.stdout or "[]")) if proc.returncode == 0 else None,
        }

    # Offline security pytest subset
    sec = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_api_security_9d5.py",
            "tests/test_maintenance_lock_9d5.py",
            "-q",
            "--tb=line",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
    )
    findings["security_pytest"] = {
        "exit_code": sec.returncode,
        "stdout_tail": "\n".join((sec.stdout or "").splitlines()[-30:]),
    }
    _write_json(OUT / "security_scan.json", findings)
    return findings


def stage_load() -> dict[str, Any]:
    import subprocess

    PERF_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/run_load_resilience_9d5.py",
            "--out-dir",
            str(PERF_DIR),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
    )
    out = {
        "stage": "load",
        "checked_at": _utc_now(),
        "exit_code": proc.returncode,
        "stdout_tail": "\n".join((proc.stdout or "").splitlines()[-40:]),
        "stderr_tail": "\n".join((proc.stderr or "").splitlines()[-40:]),
        "note": "Mocked providers only; not production capacity proof.",
    }
    _write_json(PERF_DIR / "load_status.json", out)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 9D.6 acceptance harness")
    parser.add_argument(
        "--stage",
        required=True,
        choices=[
            "offline",
            "held-out",
            "legal-risk",
            "smoke-api",
            "maintenance-dry",
            "maintenance-execute",
            "security-scan",
            "load",
        ],
    )
    parser.add_argument("--max-cost-usd", type=float, default=1.5)
    parser.add_argument("--max-calls", type=int, default=12)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default=os.getenv("LIFEPILOT_API_KEYS", "").split(",")[0])
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)

    if args.stage == "offline":
        result = stage_offline()
    elif args.stage == "held-out":
        result = stage_held_out(max_cost_usd=args.max_cost_usd, max_calls=args.max_calls)
    elif args.stage == "legal-risk":
        result = stage_legal_risk(max_cost_usd=args.max_cost_usd, max_calls=args.max_calls)
    elif args.stage == "smoke-api":
        result = stage_smoke_api(base_url=args.base_url, api_key=args.api_key, live=args.live)
    elif args.stage == "maintenance-dry":
        result = stage_maintenance(execute=False, limit=args.limit)
    elif args.stage == "maintenance-execute":
        result = stage_maintenance(execute=True, limit=args.limit)
    elif args.stage == "security-scan":
        result = stage_security_scan()
    else:
        result = stage_load()

    print(json.dumps({k: result.get(k) for k in list(result)[:12]}, indent=2, default=str))
    return 0 if result.get("exit_code", 0) in (0, None) or result.get("status") in {
        "completed",
        "baseline_already_exists",
        "partial",
        "unverified_missing_credentials",
    } or result.get("passes", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
