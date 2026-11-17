"""Deterministic Phase 9D.4 microbenchmarks (no paid LLM)."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.agent.answer import SYSTEM_PROMPT, build_user_prompt, pack_evidence_context
from lifepilot.agent.evidence import evidence_from_internal_passage
from lifepilot.agent.retrieval_policy import (
    ExternalMode,
    assess_internal_freshness,
    decide_retrieval,
    detect_high_risk_categories,
)

OUT_DIR = ROOT / "data" / "runs" / "performance_optimization_9d4"


def _passage(n: int, text: str) -> object:
    return evidence_from_internal_passage(
        jurisdiction="GB",
        official_url=f"https://www.gov.uk/skilled-worker-visa#p{n}",
        document_id=100 + n,
        document_version_id=200 + n,
        chunk_id=n,
        content_text=text,
        score=0.9,
    )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    long = (
        "Skilled Worker visa guidance. The general salary threshold is £41,700 "
        "per year or the going rate. " * 40
    )
    evidence = [
        _passage(1, long),
        _passage(2, long.replace("£41,700", "£41,700") + " Cookie preferences accept all."),
        _passage(3, "Certificate of Sponsorship must be issued by an approved sponsor."),
        _passage(4, long[:500]),
        _passage(5, "Healthcare surcharge and application fees are listed separately."),
    ]
    # Near-duplicate of passage 1 with same URL family text.
    evidence.append(
        _passage(6, long[:800] + " Additional boilerplate navigation menu links.")
    )

    t0 = time.perf_counter()
    packed = pack_evidence_context(
        evidence,
        max_items=12,
        max_chars_per_passage=1200,
        max_total_chars=14000,
    )
    prompt = build_user_prompt(
        question="What is the current Skilled Worker salary threshold?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        user_circumstances=None,
        evidence_payload=packed,
    )
    pack_ms = (time.perf_counter() - t0) * 1000

    questions = [
        "What does a Certificate of Sponsorship mean?",
        "What is the current Skilled Worker minimum salary?",
        "How much is the UK visa application fee?",
        "Can students work during term time?",
        "Explain the difference between Critical Skills and General Employment Permit pay rules.",
        "What are the updated rules for Skilled Worker visas?",
        "What was the Skilled Worker salary threshold in 2020?",
    ]
    decisions = []
    for q in questions:
        cats = detect_high_risk_categories(q)
        freshness = assess_internal_freshness(
            urls=["https://www.gov.uk/skilled-worker-visa"],
            version_retrieved_at={},
            version_ids=[],
            question_categories=cats,
        )
        d = decide_retrieval(
            question=q,
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            external_mode=ExternalMode.AUTO,
            substantive_internal_count=3,
            internal_status="ok",
            freshness=freshness,
            connectors_available=True,
        )
        decisions.append(
            {
                "question": q,
                "categories": cats,
                "run_external": d.run_external,
                "decision": d.decision.value,
                "reasons": list(d.reasons),
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "system_prompt_chars": len(SYSTEM_PROMPT),
        "packed_items": len(packed),
        "user_prompt_chars": len(prompt),
        "approx_prompt_tokens_est": (len(SYSTEM_PROMPT) + len(prompt)) // 4,
        "pack_ms": round(pack_ms, 3),
        "policy_decisions": decisions,
        "external_rate_on_sample": round(
            sum(1 for d in decisions if d["run_external"]) / len(decisions), 4
        ),
    }
    path = OUT_DIR / "microbench.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
