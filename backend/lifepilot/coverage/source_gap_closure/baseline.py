"""Phase 6F.2 baseline snapshot (reconcile 6F.1 artifacts with live DB)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from lifepilot.db.session import get_session_factory

EXPECTED_FROM_6F1 = {
    "approved_sources": 34,
    "documents": 301,
    "document_versions": 368,
    "success_extractions": 366,
    "provisional_ready": [
        "gb_skilled_worker",
        "nl_naturalisation",
        "sg_employment_pass",
    ],
    "human_verified_ready": [],
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
                      (SELECT count(*) FROM document_extractions WHERE status='success')
                        AS success_extractions
                    """
                )
            ).mappings().one()
        )
        ica_patterns = session.execute(
            text("SELECT allowed_url_patterns FROM sources WHERE id=9079")
        ).scalar()
    finally:
        session.close()

    diffs: dict[str, Any] = {}
    for key in (
        "approved_sources",
        "documents",
        "document_versions",
        "success_extractions",
    ):
        expected = EXPECTED_FROM_6F1[key]
        actual = int(live[key])
        if actual != expected:
            diffs[key] = {"expected_6f1": expected, "actual": actual}

    ready_6f1: list[str] = []
    readiness_path = runs / "phase_6f1_readiness.json"
    if readiness_path.is_file():
        ready_6f1 = json.loads(readiness_path.read_text(encoding="utf-8")).get(
            "provisional_ready_after_6f1", []
        )

    return {
        "phase": "6F.2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "expected_from_6f1": EXPECTED_FROM_6F1,
        "live_database_at_baseline_call": live,
        "differences_from_6f1_expected": diffs,
        "phase_6f1_provisional_ready": ready_6f1,
        "source_9079_allowlist_at_call": ica_patterns,
        "note": (
            "If ICA PDF was already ingested before this snapshot, document counts "
            "may exceed the 6F.1 expected baseline; report differences, do not force."
        ),
    }
