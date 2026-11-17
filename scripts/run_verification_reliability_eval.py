"""Run Phase 9D.3 verification reliability suite (offline; no paid LLM)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.eval.verification_reliability import (  # noqa: E402
    DEFAULT_FIXTURE_PATH,
    run_suite,
)

OUT_DIR = ROOT / "data" / "runs" / "verification_reliability_eval"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURE_PATH)
    args = parser.parse_args(argv)

    results, summary = run_suite(args.fixtures)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "id": r.fixture_id,
            "kind": r.kind,
            "category": r.category,
            "passed": r.passed,
            "expected": r.expected_statuses,
            "actual": r.actual_statuses,
            "leaks": r.forbidden_leaks,
            "answer": r.answer,
            "outcomes": r.outcomes,
            "notes": r.notes,
        }
        for r in results
    ]
    (OUT_DIR / "results.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixtures_path": str(args.fixtures),
        "summary": summary,
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"n={summary['totals']['n']} passed={summary['totals']['passed']} "
        f"failed={summary['totals']['failed']}"
    )
    print("synthetic", summary.get("synthetic"))
    print("real_official", summary.get("real_official"))
    if summary.get("failures"):
        print("failures:")
        for fail in summary["failures"][:20]:
            print(
                f"  {fail['id']} exp={fail['expected']} act={fail['actual']} "
                f"leaks={fail['leaks']}"
            )
    return 0 if summary["totals"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
