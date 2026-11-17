"""Phase 9D.3 offline verification reliability suite."""

from __future__ import annotations

from pathlib import Path

from lifepilot.eval.verification_reliability import (
    DEFAULT_FIXTURE_PATH,
    load_fixture_suite,
    run_suite,
)

ROOT = Path(__file__).resolve().parents[1]


def test_fixture_suite_shape_and_kinds():
    suite = load_fixture_suite()
    fixtures = suite.get("fixtures") or []
    assert 60 <= len(fixtures) <= 80
    kinds = {f.get("kind") for f in fixtures}
    assert "synthetic" in kinds
    assert "real_official" in kinds
    assert DEFAULT_FIXTURE_PATH.is_file()
    # Held-out X16–X20 must not be used as tuning targets here.
    ids = {f.get("id") for f in fixtures}
    assert not ({"X16", "X17", "X18", "X19", "X20"} & ids)


def test_verification_reliability_suite_all_pass():
    results, summary = run_suite()
    assert summary["totals"]["n"] == len(results)
    assert summary["totals"]["failed"] == 0, summary.get("failures")
    assert summary["synthetic"]["unsafe_detection"]["false_negative_rate"] == 0.0
    assert summary["real_official"]["pass_rate"] == 1.0


def test_artifacts_dir_writable_after_eval_script():
    # Ensure the canonical fixture path remains under versioned config.
    rel = DEFAULT_FIXTURE_PATH.relative_to(ROOT)
    assert str(rel).replace("\\", "/").startswith("config/eval/")
