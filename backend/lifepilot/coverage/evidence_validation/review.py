"""Build the Phase 6F.0 evidence review pack from matrix + decisions."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from lifepilot.coverage.evidence_validation.decisions import (
    CRITICAL_FIELDS,
    NEAR_READY_PATHWAYS,
    READY_PATHWAYS,
    FieldDecision,
    build_seed_decisions,
)
from lifepilot.coverage.normalization.normalize import load_normalization_config
from lifepilot.db.session import get_session_factory


TARGET_NORMALIZED = set(READY_PATHWAYS) | set(NEAR_READY_PATHWAYS)


def _load_matrix(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _original_to_normalized() -> dict[str, str]:
    cfg = load_normalization_config()
    return {
        e["original_pathway_id"]: e["normalized_id"] for e in cfg.get("entities", [])
    }


def _lookup_version_ids(
    session: Session | None,
    document_ids: set[int],
) -> dict[int, int]:
    if session is None or not document_ids:
        return {}
    ids = list(document_ids)
    if not ids:
        return {}
    rows = session.execute(
        text(
            "SELECT document_id, id AS version_id FROM document_versions "
            "WHERE is_current IS TRUE AND document_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).mappings().all()
    return {int(r["document_id"]): int(r["version_id"]) for r in rows}


def build_evidence_review(
    matrix_path: Path,
    *,
    session: Session | None = None,
) -> list[FieldDecision]:
    matrix = _load_matrix(matrix_path)
    o2n = _original_to_normalized()
    n2o = {v: k for k, v in o2n.items()}

    # Index matrix by normalized id + field
    by_key: dict[tuple[str, str], dict[str, str]] = {}
    for row in matrix:
        oid = row.get("original_pathway_id") or row.get("pathway_id") or ""
        nid = row.get("normalized_id") or o2n.get(oid, oid)
        if nid not in TARGET_NORMALIZED and oid not in {
            n2o.get(x, x) for x in TARGET_NORMALIZED
        }:
            # also accept original ids in target set
            if oid not in READY_PATHWAYS and oid not in {
                "nl_study",
                *NEAR_READY_PATHWAYS,
                *READY_PATHWAYS,
            }:
                if nid not in TARGET_NORMALIZED:
                    continue
        if nid == "nl_study":
            nid = "nl_student_residence"
        if row["requirement_field"] not in CRITICAL_FIELDS:
            continue
        # Skip financial when not applicable in matrix
        by_key[(nid, row["requirement_field"])] = row

    seeds = {
        (d.pathway_id, d.requirement): d for d in build_seed_decisions()
        if d.previous_classification != "PENDING_MATRIX"
        and d.proposed_classification != "PENDING_MATRIX"
    }

    own_session = False
    db = session
    if db is None:
        try:
            db = get_session_factory()()
            own_session = True
        except Exception:  # noqa: BLE001
            db = None

    try:
        doc_ids = {d.document_id for d in seeds.values() if d.document_id}
        # also from matrix document_id column if present
        for row in by_key.values():
            if row.get("document_id") and str(row["document_id"]).isdigit():
                doc_ids.add(int(row["document_id"]))
        versions = _lookup_version_ids(db, {d for d in doc_ids if d})

        out: list[FieldDecision] = []
        n = 0
        # Prefer seed overrides for covered keys; fill remaining from matrix
        covered_keys = set(by_key.keys())
        # Ensure all target pathway critical fields from matrix
        ordered_keys: list[tuple[str, str]] = []
        for nid in list(READY_PATHWAYS) + list(NEAR_READY_PATHWAYS):
            for field in CRITICAL_FIELDS:
                if (nid, field) in by_key:
                    ordered_keys.append((nid, field))
                elif nid == "nl_student_residence" and ("nl_study", field) in {
                    (o2n.get(k[0], k[0]), k[1]) for k in []
                }:
                    pass

        # Rebuild ordered from matrix rows for targets
        ordered_keys = []
        for nid in list(READY_PATHWAYS) + list(NEAR_READY_PATHWAYS):
            for field in CRITICAL_FIELDS:
                row = by_key.get((nid, field))
                if row is None and nid == "nl_student_residence":
                    row = by_key.get(("nl_study", field))
                if row is None:
                    continue
                ordered_keys.append((nid, field))
                by_key[(nid, field)] = row

        for nid, field in ordered_keys:
            row = by_key[(nid, field)]
            prev = row["coverage_class"]
            applicable = str(row.get("applicable", "True")).lower() in {
                "1",
                "true",
                "yes",
            }
            if not applicable and field == "financial_or_sponsorship":
                # Still include with NOT_APPLICABLE if matrix says so
                pass

            seed = seeds.get((nid, field))
            n += 1
            rid = f"F0-{n:03d}"
            if seed:
                decision = FieldDecision(
                    review_id=rid,
                    country=seed.country,
                    pathway_id=seed.pathway_id,
                    original_pathway_id=seed.original_pathway_id,
                    requirement=seed.requirement,
                    previous_classification=prev,
                    proposed_classification=seed.proposed_classification,
                    evidence_quality_issue=seed.evidence_quality_issue,
                    evidence_category=seed.evidence_category,
                    source_url=seed.source_url,
                    source_id=seed.source_id,
                    document_id=seed.document_id,
                    version_id=seed.version_id
                    or (versions.get(seed.document_id) if seed.document_id else None),
                    exact_supporting_passage=seed.exact_supporting_passage,
                    directly_answers=seed.directly_answers,
                    pathway_specific=seed.pathway_specific,
                    extraction_complete_enough=seed.extraction_complete_enough,
                    explanation=seed.explanation,
                    remediation_category=seed.remediation_category,
                    reviewer_status="automated_provisional",
                    human_reviewed_classification="",
                    uncertainty=seed.uncertainty,
                )
            else:
                excerpt = row.get("evidence_excerpt") or ""
                doc_id = None
                if row.get("document_id") and str(row["document_id"]).isdigit():
                    doc_id = int(row["document_id"])
                ver_id = None
                if row.get("version_id") and str(row["version_id"]).isdigit():
                    ver_id = int(row["version_id"])
                elif doc_id:
                    ver_id = versions.get(doc_id)
                oid = row.get("original_pathway_id") or n2o.get(nid, nid)
                decision = FieldDecision(
                    review_id=rid,
                    country=row.get("country") or "",
                    pathway_id=nid,
                    original_pathway_id=oid,
                    requirement=field,
                    previous_classification=prev,
                    proposed_classification=prev,
                    evidence_quality_issue="carried_forward_pending_deep_spotcheck",
                    evidence_category="D",
                    source_url="",
                    source_id=0,
                    document_id=doc_id,
                    version_id=ver_id,
                    exact_supporting_passage=excerpt[:500],
                    directly_answers=prev == "COMPLETE",
                    pathway_specific=True,
                    extraction_complete_enough=prev == "COMPLETE",
                    explanation=(
                        "No curated override; carried forward from Phase 6E.1 calibrated "
                        "matrix pending human review."
                    ),
                    remediation_category="human_verification",
                    reviewer_status="automated_provisional",
                    uncertainty="Not independently deep-inspected in 6F.0 seed set.",
                )
            out.append(decision)
        return out
    finally:
        if own_session and db is not None:
            db.close()


def write_review_csv(path: Path, decisions: list[FieldDecision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not decisions:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(decisions[0].to_csv_row().keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for d in decisions:
            writer.writerow(d.to_csv_row())
