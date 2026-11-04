"""Country coverage matrix and priority backlog."""

from __future__ import annotations

from typing import Any

from lifepilot.coverage.pathway_inventory.categories import CATEGORIES, JURISDICTIONS
from lifepilot.coverage.pathway_inventory.inventory import inventory_summary


def build_country_coverage(
    inventory_rows: list[dict[str, Any]],
    evidence_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    inv = inventory_summary(inventory_rows)
    by_country_inv = inv["by_country"]

    evidence_by_country: dict[str, dict[str, int]] = {}
    for r in evidence_rows:
        if r["entity_type"] != "true_pathway":
            continue
        c = r["country"]
        bucket = evidence_by_country.setdefault(
            c,
            {"cells": 0, "complete": 0, "partial": 0, "missing": 0, "unevaluated": 0},
        )
        if r["classification"] == "NOT_APPLICABLE":
            continue
        bucket["cells"] += 1
        cls = r["classification"].lower()
        if cls in bucket:
            bucket[cls] += 1
        if r["classification"] == "COMPLETE":
            bucket["complete"] += 1

    out: list[dict[str, Any]] = []
    for country in JURISDICTIONS:
        bi = by_country_inv.get(
            country,
            {
                "true_pathways": 0,
                "hubs": 0,
                "unknown_candidates": 0,
                "with_approved_source": 0,
                "with_ingested_docs": 0,
            },
        )
        be = evidence_by_country.get(
            country,
            {"cells": 0, "complete": 0, "partial": 0, "missing": 0, "unevaluated": 0},
        )
        true_n = bi["true_pathways"]
        out.append(
            {
                "country": country,
                "true_pathways_identified": true_n,
                "information_hubs": bi.get("hubs", 0),
                "unknown_candidates": bi.get("unknown_candidates", 0),
                "true_pathways_with_approved_source": bi.get("with_approved_source", 0),
                "true_pathways_with_ingested_docs": bi.get("with_ingested_docs", 0),
                "inventory_coverage_rate": (
                    round(bi.get("with_approved_source", 0) / true_n, 4) if true_n else 0.0
                ),
                "inventory_denominator": "true_pathways_identified",
                "ingestion_coverage_rate": (
                    round(bi.get("with_ingested_docs", 0) / true_n, 4) if true_n else 0.0
                ),
                "ingestion_denominator": "true_pathways_identified",
                "applicable_evidence_cells": be["cells"],
                "complete_evidence_cells": be["complete"],
                "evidence_complete_rate": (
                    round(be["complete"] / be["cells"], 4) if be["cells"] else 0.0
                ),
                "evidence_denominator": "applicable critical cells on true pathways",
                "categories_represented": ",".join(
                    sorted(
                        {
                            r["category"]
                            for r in inventory_rows
                            if r["country"] == country and r["entity_type"] != "unknown_candidate"
                        }
                    )
                ),
                "weakest_signal": (
                    "no_approved_source"
                    if bi.get("with_approved_source", 0) == 0
                    else (
                        "low_ingestion"
                        if bi.get("with_ingested_docs", 0) < max(1, true_n // 3)
                        else "evidence_gaps"
                    )
                ),
            }
        )
    return out


def build_priority_backlog(
    country_rows: list[dict[str, Any]],
    approval_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    backlog: list[dict[str, Any]] = []
    # Country-level priorities
    for row in sorted(
        country_rows,
        key=lambda r: (
            0 if r["true_pathways_with_approved_source"] == 0 else 1,
            r["ingestion_coverage_rate"],
            r["evidence_complete_rate"],
        ),
    ):
        backlog.append(
            {
                "item_type": "country_coverage",
                "country": row["country"],
                "pathway_id": "",
                "priority": (
                    1
                    if row["true_pathways_with_approved_source"] == 0
                    else (2 if row["ingestion_coverage_rate"] < 0.35 else 3)
                ),
                "action": (
                    "approve_new_official_sources"
                    if row["true_pathways_with_approved_source"] == 0
                    else "expand_allowlist_and_ingest"
                ),
                "rationale": (
                    f"true_pathways={row['true_pathways_identified']}; "
                    f"approved_source_link={row['true_pathways_with_approved_source']}; "
                    f"ingested={row['true_pathways_with_ingested_docs']}; "
                    f"evidence_complete_rate={row['evidence_complete_rate']}"
                ),
                "estimated_effort": "L" if row["true_pathways_with_approved_source"] == 0 else "M",
            }
        )

    for a in approval_rows:
        if a.get("approval_status") in {
            "pending_human_approval",
            "pending_new_source_approval",
            "pending_allowlist_expansion",
        }:
            backlog.append(
                {
                    "item_type": "approval_queue",
                    "country": a.get("jurisdiction"),
                    "pathway_id": a.get("pathway"),
                    "priority": a.get("priority", 2),
                    "action": a.get("approval_status"),
                    "rationale": a.get("url") or a.get("discovery_origin"),
                    "estimated_effort": "S",
                }
            )
    return backlog


def category_unknown_counts(inventory_rows: list[dict[str, Any]]) -> dict[str, Any]:
    unknown = [
        r for r in inventory_rows if r["entity_type"] == "unknown_candidate"
    ]
    return {
        "unknown_candidate_entities": len(unknown),
        "by_country": {
            c: sum(1 for r in unknown if r["country"] == c) for c in JURISDICTIONS
        },
        "categories": list(CATEGORIES),
        "note": (
            "Unknown candidates remain in the denominator for inventory completeness; "
            "NOT_OFFERED is unused unless authoritative evidence exists."
        ),
    }
