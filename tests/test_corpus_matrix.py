"""Coverage/evidence matrix helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from lifepilot.coverage.corpus_reliability.matrix_io import (
    build_coverage_matrix,
    coverage_snapshot,
    load_csv,
    verify_matrix_cardinality,
)
from lifepilot.coverage.corpus_reliability.readiness import pathway_is_provisionally_ready
from lifepilot.coverage.corpus_reliability.source_authorization import (
    EXPLICITLY_NOT_APPROVED,
)


def test_evidence_matrix_cardinality_when_present():
    path = Path("data/runs/evidence_matrix.csv")
    if not path.is_file():
        pytest.skip("evidence matrix not present")
    rows = load_csv(path)
    card = verify_matrix_cardinality(rows)
    assert card["ok"] is True
    assert card["true_pathway_cells"] == 510


def test_coverage_snapshot_counts_match_rows():
    path = Path("data/runs/evidence_matrix.csv")
    if not path.is_file():
        pytest.skip("evidence matrix not present")
    rows = load_csv(path)
    snap = coverage_snapshot(rows)
    true = [r for r in rows if r.get("entity_type") == "true_pathway"]
    assert sum(snap["classification_counts"].values()) == len(true)


def test_coverage_matrix_unique_keys():
    path = Path("data/runs/coverage_matrix.csv")
    if not path.is_file():
        ev = Path("data/runs/evidence_matrix.csv")
        if not ev.is_file():
            pytest.skip("matrices not present")
        rows = load_csv(ev)
        cov = build_coverage_matrix(rows)
    else:
        cov = load_csv(path)
    keys = [(r.get("pathway_id"), r.get("requirement")) for r in cov]
    assert len(keys) == 510
    assert len(keys) == len(set(keys))


def test_uae_icp_excluded():
    assert any("icp.gov.ae/en/services" in u for u in EXPLICITLY_NOT_APPROVED)


def test_quality_ready_subset_of_classification_ready():
    path = Path("data/runs/evidence_matrix.csv")
    if not path.is_file():
        pytest.skip("evidence matrix not present")
    rows = load_csv(path)
    snap = coverage_snapshot(rows)
    quality = [
        pid for pid in sorted({r["pathway_id"] for r in rows if r.get("entity_type") == "true_pathway"})
        if pathway_is_provisionally_ready(rows, pid)
    ]
    # Quality-ready must be subset of classification-complete pathways
    assert set(quality) <= set(snap["provisionally_complete_pathways"])
