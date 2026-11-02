"""Live database baseline snapshot for Phase 6F.1."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from lifepilot.db.session import get_session_factory

EXPECTED_BASELINE = {
    "approved_sources": 34,
    "documents": 301,
    "document_versions": 368,
    "success_extractions": 366,
    "normalized_true_pathways_6e1": 26,
    "information_hubs_6e1": 19,
    "unknown_slots_6e1": 42,
    "provisional_ready_6f0": ["gb_skilled_worker"],
    "human_verified_ready": [],
    "critical_fields_reviewed_6f0": 55,
    "questionable_matches_6f0": 18,
    "false_positive_complete_6f0": 7,
}


def capture_baseline(*, runs_dir: Path | None = None) -> dict[str, Any]:
    runs = runs_dir or Path(__file__).resolve().parents[4] / "data" / "runs"
    session = get_session_factory()()
    try:
        live = dict(
            session.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM sources WHERE review_status='approved')
                        AS approved_sources,
                      (SELECT count(*) FROM documents) AS documents,
                      (SELECT count(*) FROM document_versions) AS document_versions,
                      (SELECT count(*) FROM document_versions WHERE is_current)
                        AS current_versions,
                      (SELECT count(*) FROM document_extractions WHERE status='success')
                        AS success_extractions
                    """
                )
            ).mappings().one()
        )
    finally:
        session.close()

    diffs: dict[str, Any] = {}
    for key in (
        "approved_sources",
        "documents",
        "document_versions",
        "success_extractions",
    ):
        expected = EXPECTED_BASELINE[key]
        actual = int(live[key])
        if actual != expected:
            diffs[key] = {"expected": expected, "actual": actual}

    norm_path = runs / "phase_6e1_normalized_pathways.json"
    readiness_6f0 = runs / "phase_6f0_readiness.json"
    norm_counts = {}
    if norm_path.is_file():
        norm_counts = json.loads(norm_path.read_text(encoding="utf-8")).get("counts", {})
    ready_6f0 = []
    if readiness_6f0.is_file():
        ready_6f0 = json.loads(readiness_6f0.read_text(encoding="utf-8")).get(
            "previously_ready_still_provisional_ready", []
        )

    return {
        "phase": "6F.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "expected_from_6f0_brief": EXPECTED_BASELINE,
        "live_database": live,
        "differences_from_expected": diffs,
        "phase_6e1_normalized_counts": norm_counts,
        "phase_6f0_provisional_ready": ready_6f0,
        "note": (
            "Differences are reported, not forced. Historical Phase 6E/6E.1/6F.0 "
            "artifacts are not modified."
        ),
    }
