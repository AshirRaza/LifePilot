"""Evidence/coverage matrix I/O and cardinality helpers."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifepilot.coverage.corpus_reliability.assessment import get_classification

REQUIREMENTS = (
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
)

EVIDENCE_MATRIX_NAME = "evidence_matrix.csv"
COVERAGE_MATRIX_NAME = "coverage_matrix.csv"
STATUS_REPORT_NAME = "corpus_status_report.md"
BLOCKED_RECOVERIES_NAME = "blocked_evidence_recoveries.csv"
BACKUP_MANIFEST_NAME = "backup_manifest.json"


def load_csv(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                fields.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# Back-compat aliases used by older call sites during transition.
_load_csv = load_csv
_write_csv = write_csv


def evidence_matrix_path(runs_dir: Path) -> Path:
    return runs_dir / EVIDENCE_MATRIX_NAME


def coverage_matrix_path(runs_dir: Path) -> Path:
    return runs_dir / COVERAGE_MATRIX_NAME


def coverage_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    true_rows = [r for r in rows if r.get("entity_type") == "true_pathway"]
    classes = Counter(get_classification(r) for r in true_rows)
    by: dict[str, dict[str, str]] = {}
    for r in true_rows:
        by.setdefault(r["pathway_id"], {})[r["requirement"]] = get_classification(r)
    ready = [
        pid for pid, m in by.items()
        if all(m.get(x) in ("COMPLETE", "NOT_APPLICABLE") for x in REQUIREMENTS)
    ]
    return {
        "classification_counts": dict(classes),
        "provisionally_complete_pathways": sorted(ready),
        "unevaluated": classes.get("UNEVALUATED", 0),
        "partial": classes.get("PARTIAL", 0),
        "complete": classes.get("COMPLETE", 0),
        "missing": classes.get("MISSING", 0),
        "not_applicable": classes.get("NOT_APPLICABLE", 0),
    }


def verify_matrix_cardinality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    true = [r for r in rows if r.get("entity_type") == "true_pathway"]
    keys = [(r["pathway_id"], r["requirement"]) for r in true]
    dups = [k for k, n in Counter(keys).items() if n > 1]
    pathways = sorted({r["pathway_id"] for r in true})
    by_p: dict[str, set[str]] = defaultdict(set)
    for r in true:
        by_p[r["pathway_id"]].add(r["requirement"])
    incomplete = {
        p: sorted(set(REQUIREMENTS) - s)
        for p, s in by_p.items()
        if set(REQUIREMENTS) - s
    }
    return {
        "true_pathway_cells": len(true),
        "expected_cells": 102 * 5,
        "pathways": len(pathways),
        "unique_keys": len(set(keys)),
        "duplicates": dups,
        "incomplete_pathways": incomplete,
        "ok": (
            len(true) == 510
            and len(pathways) == 102
            and not dups
            and not incomplete
        ),
    }


def build_coverage_matrix(
    rows: list[dict[str, Any]],
    partial_audits: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    audit_index = {
        (a["pathway_id"], a["requirement"]): a for a in (partial_audits or [])
    }
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("entity_type") != "true_pathway":
            continue
        pid, req = row["pathway_id"], row["requirement"]
        cls = get_classification(row)
        audit = audit_index.get((pid, req), {})
        quality = audit.get("finding") or (
            "COMPLETE_CELL" if cls == "COMPLETE"
            else "NOT_APPLICABLE" if cls == "NOT_APPLICABLE"
            else "MISSING_COVERAGE" if cls == "MISSING"
            else "UNEVALUATED_COVERAGE" if cls == "UNEVALUATED"
            else "SUPPORTED_PARTIAL" if cls == "PARTIAL"
            else "UNKNOWN"
        )
        gap_reason = ""
        next_action = "none"
        if cls == "COMPLETE":
            next_action = "retain_pending_human_review"
        elif cls == "NOT_APPLICABLE":
            next_action = "retain_justified_na"
            gap_reason = row.get("not_applicable_rationale") or "Justified not applicable"
        elif cls == "PARTIAL":
            next_action = "strengthen_with_checklist_or_criteria_page"
            gap_reason = audit.get("rationale") or "Partial support only"
        elif cls in ("MISSING", "UNEVALUATED"):
            next_action = "discover_or_recover_official_source"
            gap_reason = (
                "No validated association"
                if cls == "MISSING"
                else "Not meaningfully assessed or cleared after audit"
            )
        if audit.get("action") == "invalidate":
            next_action = "replace_invalid_evidence"
            gap_reason = audit.get("rationale") or gap_reason

        out.append({
            "pathway_id": pid,
            "country": row.get("country") or "",
            "requirement": req,
            "classification": cls,
            "document_id": row.get("document_id") or "",
            "version_id": row.get("version_id") or "",
            "source_url": row.get("source_url") or "",
            "method": row.get("method") or "",
            "quality_assessment": quality,
            "gap_reason": gap_reason,
            "recommended_next_action": next_action,
            "passage_excerpt": (row.get("passage") or "")[:160],
            "pathway_provisionally_ready": row.get("pathway_provisionally_ready") or "",
        })
    out.sort(key=lambda r: (r["country"], r["pathway_id"], r["requirement"]))
    return out


def load_extraction(
    session: Session,
    data_dir: Path,
    document_id: int,
) -> tuple[int | None, str, list[dict[str, Any]], str]:
    row = session.execute(
        text(
            """
            SELECT dv.id AS version_id, d.canonical_url, d.title, de.storage_path
            FROM documents d
            JOIN document_versions dv ON dv.document_id = d.id AND dv.is_current = true
            JOIN document_extractions de ON de.document_version_id = dv.id AND de.status = 'success'
            WHERE d.id = :doc_id
            LIMIT 1
            """
        ),
        {"doc_id": document_id},
    ).mappings().first()
    if not row:
        return None, "", [], ""
    path = data_dir / row["storage_path"]
    if not path.is_file():
        alt = Path(row["storage_path"])
        path = alt if alt.is_file() else path
    blocks: list[dict[str, Any]] = []
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            blocks = payload.get("blocks") or payload.get("content", {}).get("blocks", [])
        except (OSError, json.JSONDecodeError):
            blocks = []
    return int(row["version_id"]), row["canonical_url"], blocks, row.get("title") or ""


# Alias used by older call sites.
_load_extraction = load_extraction


def country_doc_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        text(
            """
            SELECT coalesce(j.code, '??') AS code, count(d.id) AS n
            FROM documents d
            LEFT JOIN sources s ON s.id = d.primary_source_id
            LEFT JOIN jurisdictions j ON j.id = s.jurisdiction_id
            GROUP BY 1 ORDER BY 1
            """
        )
    ).mappings().all()
    return {r["code"]: int(r["n"]) for r in rows}


def is_eu_framework_url(url: str) -> bool:
    u = (url or "").lower()
    return "europa.eu/youreurope" in u or "eur-lex.europa.eu" in u


def pathways_for_url(url: str) -> list[str]:
    from lifepilot.coverage.corpus_reliability.pathway_applicability import PATHWAY_URL_TOKENS

    if is_eu_framework_url(url):
        return []
    key = (url or "").lower()
    hits: list[str] = []
    for pid, tokens in PATHWAY_URL_TOKENS.items():
        for tok in tokens:
            if tok.lower() in key:
                hits.append(pid)
                break
    return list(dict.fromkeys(hits))
