"""Phase 9D.5 mocked load + resilience checks (no paid LLM by default)."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from fastapi.testclient import TestClient

from lifepilot.agent.answer import AnswerClaim
from lifepilot.agent.config import reset_agent_config_cache
from lifepilot.agent.contracts import AgentResponse, AgentStatus, TimingMetadata
from lifepilot.agent.providers import reset_llm_provider_override
from lifepilot.agent.runner import run_agent
from lifepilot.api.app import create_app
from lifepilot.api.settings import reset_api_settings_cache

OUT_DIR = ROOT / "data" / "runs" / "production_reliability_9d5"


def _fake_agent(*_args, **_kwargs) -> AgentResponse:
    time.sleep(0.01)
    return AgentResponse(
        request_id="req_load_fake",
        status=AgentStatus.SUCCESS,
        answer="Mock grounded answer for load testing.",
        claims=[
            AnswerClaim(
                claim="Mock claim",
                evidence_ids=["ev_mock_00000001"],
            )
        ],
        verification={
            "claim_outcomes": [],
            "suppressed_claims": [],
            "warnings": ["mock_verification"],
        },
        timing=TimingMetadata(total_ms=10.0, verification_ms=1.0, llm_ms=5.0),
        retrieval_decision="internal_only",
        jurisdiction="GB",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--api-key", default="load-test-key")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["LIFEPILOT_ENV"] = "development"
    os.environ["LIFEPILOT_API_REQUIRE_AUTH"] = "1"
    os.environ["LIFEPILOT_API_KEYS"] = args.api_key
    os.environ["LIFEPILOT_API_RATE_LIMIT_PER_MINUTE"] = "1000"
    os.environ["LIFEPILOT_API_MAX_CONCURRENT_ANSWERS"] = str(max(2, args.concurrency // 2))
    os.environ["LIFEPILOT_VERIFICATION_ENABLED"] = "1"
    reset_api_settings_cache()
    reset_agent_config_cache()
    reset_llm_provider_override()

    import lifepilot.api.app as api_app
    import lifepilot.agent.runner as runner_mod

    original = runner_mod.run_agent
    runner_mod.run_agent = _fake_agent  # type: ignore[assignment]
    api_app.run_agent = _fake_agent  # type: ignore[assignment]

    try:
        app = create_app()
        client = TestClient(app)
        headers = {"X-API-Key": args.api_key}
        assert client.get("/health").status_code == 200

        latencies: list[float] = []
        errors = 0
        statuses: dict[int, int] = {}
        lock = threading.Lock()

        def one(i: int) -> dict[str, Any]:
            started = time.perf_counter()
            resp = client.post(
                "/v1/answer",
                headers=headers,
                json={
                    "question": f"Mock load question {i} about Skilled Worker visas?",
                    "jurisdiction": "GB",
                    "pathway_id": "gb_skilled_worker",
                },
            )
            elapsed = (time.perf_counter() - started) * 1000
            return {"status_code": resp.status_code, "elapsed_ms": elapsed}

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(one, i) for i in range(args.requests)]
            for fut in as_completed(futures):
                row = fut.result()
                with lock:
                    latencies.append(row["elapsed_ms"])
                    statuses[row["status_code"]] = statuses.get(row["status_code"], 0) + 1
                    if row["status_code"] >= 400:
                        errors += 1

        # Auth negative
        unauth = client.post(
            "/v1/answer",
            json={"question": "x", "jurisdiction": "GB"},
        )
        # Oversize question
        big = client.post(
            "/v1/answer",
            headers=headers,
            json={"question": "q" * 5000, "jurisdiction": "GB"},
        )
        # DB readiness may fail without DATABASE_URL in some envs — capture only.
        ready = client.get("/ready")

        summary = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "mocked_agent",
            "requests": args.requests,
            "concurrency": args.concurrency,
            "error_count": errors,
            "status_counts": statuses,
            "latency_ms": {
                "median": round(statistics.median(latencies), 2) if latencies else None,
                "p95": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 2)
                if latencies
                else None,
                "mean": round(statistics.mean(latencies), 2) if latencies else None,
            },
            "auth_rejected_status": unauth.status_code,
            "oversize_status": big.status_code,
            "ready_status": ready.status_code,
            "note": (
                "Throughput is mocked (no Claude). Do not treat as production SLA."
            ),
        }
        path = out_dir / "load_resilience_summary.json"
        path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2))
        return 0 if errors == 0 and unauth.status_code == 401 else 1
    finally:
        runner_mod.run_agent = original  # type: ignore[assignment]
        api_app.run_agent = original  # type: ignore[assignment]
        reset_api_settings_cache()


if __name__ == "__main__":
    raise SystemExit(main())
