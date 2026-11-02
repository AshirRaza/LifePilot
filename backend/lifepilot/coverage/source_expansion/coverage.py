"""Country coverage (corrected metrics + evidence cells) and priority backlog for 6H."""

from __future__ import annotations

from typing import Any


def _evidence_by_country(evidence_rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in evidence_rows:
        if r["entity_type"] != "true_pathway":
            continue
        b = out.setdefault(
            r["country"],
            {
                "cells": 0, "complete": 0, "partial": 0, "missing": 0,
                "unevaluated": 0, "blocked": 0,
            },
        )
        if r["classification"] == "NOT_APPLICABLE":
            continue
        b["cells"] += 1
        key = r["classification"].lower()
        if key in b:
            b[key] += 1
    return out


def build_country_coverage_6h(
    country_metrics: list[dict[str, Any]],
    evidence_rows: list[dict[str, Any]],
    prior_6g_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ev = _evidence_by_country(evidence_rows)
    prior = {r["country"]: r for r in prior_6g_rows}
    zero = {"cells": 0, "complete": 0, "partial": 0, "missing": 0, "unevaluated": 0, "blocked": 0}
    out: list[dict[str, Any]] = []
    for m in country_metrics:
        e = ev.get(m["country"], zero)
        p = prior.get(m["country"], {})
        n = e["cells"]
        row = dict(m)
        row.update(
            {
                "applicable_evidence_cells": n,
                "complete_evidence_cells": e["complete"],
                "partial_evidence_cells": e["partial"],
                "missing_evidence_cells": e["missing"],
                "unevaluated_evidence_cells": e["unevaluated"],
                "blocked_evidence_cells": e["blocked"],
                "evidence_complete_rate": round(e["complete"] / n, 4) if n else 0.0,
                "evidence_denominator": "applicable critical cells on true pathways (excl. NOT_APPLICABLE)",
                "legacy_6g_with_approved_source": p.get("true_pathways_with_approved_source", ""),
                "legacy_6g_with_ingested_docs_source_wide": p.get("true_pathways_with_ingested_docs", ""),
                "legacy_6g_evidence_complete_rate": p.get("evidence_complete_rate", ""),
                "legacy_6g_applicable_cells": p.get("applicable_evidence_cells", ""),
                "legacy_6g_complete_cells": p.get("complete_evidence_cells", ""),
                "weakest_signal": _weakest(row, e),
            }
        )
        out.append(row)
    return out


def _weakest(row: dict[str, Any], e: dict[str, int]) -> str:
    if not row["country_has_approved_source"]:
        return "no_approved_source"
    if row["true_pathways"] and row["pathway_source_linked"] == 0:
        return "no_pathway_source_link"
    if row["true_pathways"] and row["pathway_relevant_document_extracted"] < max(
        1, row["true_pathways"] // 3
    ):
        return "low_pathway_relevant_extraction"
    if e["unevaluated"] > e["complete"]:
        return "unevaluated_dominates"
    return "evidence_gaps"


def build_priority_backlog_6h(
    country_rows: list[dict[str, Any]], pending_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    backlog: list[dict[str, Any]] = []
    national = [r for r in country_rows if r["scope"] == "national"]
    for r in sorted(
        national,
        key=lambda x: (
            0 if not x["country_has_approved_source"] else 1,
            x["pathway_relevant_document_extracted_rate"],
            x["evidence_complete_rate"],
        ),
    ):
        backlog.append(
            {
                "item_type": "country_coverage",
                "country": r["country"],
                "pathway_id": "",
                "priority": (
                    1 if not r["country_has_approved_source"]
                    else (2 if r["pathway_relevant_document_extracted_rate"] < 0.35 else 3)
                ),
                "action": (
                    "approve_new_official_sources"
                    if not r["country_has_approved_source"]
                    else "approve_pathway_specific_urls_and_deepen_evidence"
                ),
                "rationale": (
                    f"true_pathways={r['true_pathways']}; "
                    f"pathway_source_linked={r['pathway_source_linked']}; "
                    f"pathway_relevant_extracted={r['pathway_relevant_document_extracted']}; "
                    f"unevaluated_cells={r['unevaluated_evidence_cells']}; "
                    f"weakest={r['weakest_signal']}"
                ),
                "estimated_effort": "L" if not r["country_has_approved_source"] else "M",
            }
        )
    for a in pending_rows:
        backlog.append(
            {
                "item_type": "pending_approval",
                "country": a.get("jurisdiction"),
                "pathway_id": a.get("pathway"),
                "priority": a.get("priority", 3),
                "action": a.get("approval_status"),
                "rationale": a.get("url") or a.get("reason", ""),
                "estimated_effort": "S",
            }
        )
    backlog.sort(key=lambda r: (int(r.get("priority") or 3), r["item_type"]))
    return backlog
