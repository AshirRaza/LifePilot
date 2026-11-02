"""Corrected coverage metrics for Phase 6H.

Fixes the Phase 6G defects:

* entity arithmetic now includes ``cross_cutting_guidance`` (102+33+7+7 = 149);
* ``country_has_approved_source`` is a different statement from ``pathway_source_linked``;
* EU framework sources are reported separately from national totals;
* a pathway is "document-ingested" only when a document URL actually matches that pathway
  (URL-token scope), never because its source holds unrelated documents.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifepilot.coverage.pathway_inventory.categories import JURISDICTIONS
from lifepilot.coverage.source_expansion.relevance import match_url_to_pathways

ENTITY_TYPES = (
    "true_pathway",
    "information_hub",
    "cross_cutting_guidance",
    "unknown_candidate",
)
EXPECTED_ENTITY_TOTAL = 149
NATIONAL = tuple(c for c in JURISDICTIONS if c != "EU")


def reconcile_entities(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Entity totals including every type; asserts the sum equals the row count."""
    counts = {t: 0 for t in ENTITY_TYPES}
    other: dict[str, int] = defaultdict(int)
    for r in rows:
        et = r["entity_type"]
        if et in counts:
            counts[et] += 1
        else:
            other[et] += 1
    total = len(rows)
    arith = sum(counts.values()) + sum(other.values())
    by_country: dict[str, dict[str, int]] = {}
    for r in rows:
        b = by_country.setdefault(r["country"], {t: 0 for t in ENTITY_TYPES} | {"total": 0})
        if r["entity_type"] in b:
            b[r["entity_type"]] += 1
        b["total"] += 1
    return {
        "total_entities": total,
        "entity_type_counts": counts,
        "unrecognised_entity_types": dict(other),
        "entity_type_sum": arith,
        "reconciles": arith == total,
        "expected_total": EXPECTED_ENTITY_TOTAL,
        "matches_expected_149": total == EXPECTED_ENTITY_TOTAL,
        "formula": " + ".join(str(counts[t]) for t in ENTITY_TYPES) + f" = {arith}",
        "by_country": by_country,
        "note": (
            "6G arithmetic quoted 102+33+7=142 and omitted cross_cutting_guidance; the "
            "correct entity partition is true_pathway + information_hub + "
            "cross_cutting_guidance + unknown_candidate."
        ),
    }


def load_corpus_documents(session: Session) -> list[dict[str, Any]]:
    """All documents with current-version extraction status and country (via source)."""
    rows = session.execute(
        text(
            """
            SELECT d.id AS document_id, d.canonical_url, d.primary_source_id AS source_id,
                   d.content_kind, v.id AS version_id,
                   COALESCE(j.country_code, j.code) AS country,
                   s.review_status AS source_review_status,
                   (SELECT e.status FROM document_extractions e
                     WHERE e.document_version_id = v.id ORDER BY e.id DESC LIMIT 1)
                     AS extraction_status
            FROM documents d
            JOIN document_versions v ON v.document_id = d.id AND v.is_current
            LEFT JOIN sources s ON s.id = d.primary_source_id
            LEFT JOIN jurisdictions j ON j.id = s.jurisdiction_id
            ORDER BY d.id
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def load_approved_sources(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT s.id, s.name, s.review_status, s.seed_url,
                   COALESCE(j.country_code, j.code) AS country_code,
                   s.allowed_url_patterns
            FROM sources s
            LEFT JOIN jurisdictions j ON j.id = s.jurisdiction_id
            ORDER BY s.id
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def country_of_source(source: dict[str, Any]) -> str:
    code = source.get("country_code")
    if code in NATIONAL:
        return str(code)
    # Jurisdictions that are not one of our 13 national codes (e.g. the EU) stay separate.
    return "EU"


def build_link_overlay(
    decisions: list[dict[str, Any]], approved_source_ids: set[int]
) -> dict[str, list[dict[str, Any]]]:
    """pathway_id -> approved owner decisions that register a source for that pathway."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in decisions:
        if d.get("decision") != "approved" or not d.get("source_id"):
            continue
        sid = int(d["source_id"])
        if sid not in approved_source_ids:
            continue
        for pid in str(d.get("pathway_ids") or "").split(","):
            if pid:
                out[pid].append(
                    {
                        "source_id": sid,
                        "candidate_id": d["candidate_id"],
                        "decision_code": d["decision_code"],
                    }
                )
    return dict(out)


def pathway_document_matches(
    docs: list[dict[str, Any]], inventory_rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """URL-scope matches per true pathway (strong and shared kept distinct)."""
    out: dict[str, dict[str, Any]] = {}
    for d in docs:
        for m in match_url_to_pathways(d["canonical_url"], d.get("country"), inventory_rows):
            b = out.setdefault(
                m["pathway_id"], {"strong": [], "shared": [], "extracted": []}
            )
            b[m["strength"]].append(d["document_id"])
            if d.get("extraction_status") == "success":
                b["extracted"].append(d["document_id"])
    return out


def build_pathway_metrics(
    inventory_rows: list[dict[str, Any]],
    *,
    approved_source_ids: set[int],
    overlay: dict[str, list[dict[str, Any]]],
    doc_matches: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in inventory_rows:
        pid = r["pathway_id"]
        static_linked = sorted(int(s) for s in r.get("source_ids") or [] if int(s) in approved_source_ids)
        overlay_linked = sorted({o["source_id"] for o in overlay.get(pid, [])})
        linked_ids = sorted(set(static_linked) | set(overlay_linked))
        m = doc_matches.get(pid, {"strong": [], "shared": [], "extracted": []})
        strong = sorted(set(m["strong"]))
        shared = sorted(set(m["shared"]))
        extracted = sorted(set(m["extracted"]))
        is_true = r["entity_type"] == "true_pathway"
        out.append(
            {
                "pathway_id": pid,
                "country": r["country"],
                "entity_type": r["entity_type"],
                "pathway_source_linked": bool(linked_ids) if is_true else False,
                "linked_source_ids": linked_ids,
                "link_basis": (
                    "owner_decision_6h"
                    if overlay_linked and not static_linked
                    else ("inventory_source_ids" if static_linked else "")
                ),
                "pathway_relevant_document_ingested": bool(strong or shared) if is_true else False,
                "pathway_relevant_document_extracted": bool(extracted) if is_true else False,
                "pathway_strong_document_ids": strong,
                "pathway_shared_document_ids": shared,
                "pathway_extracted_document_ids": extracted,
                "legacy_source_wide_ingested_documents": r.get("ingested_documents", 0),
            }
        )
    return out


def build_country_metrics(
    inventory_rows: list[dict[str, Any]],
    pathway_metrics: list[dict[str, Any]],
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    approved_by_country: dict[str, int] = defaultdict(int)
    for s in sources:
        if s["review_status"] == "approved":
            approved_by_country[country_of_source(s)] += 1
    rows: list[dict[str, Any]] = []
    for country in JURISDICTIONS:
        inv = [r for r in inventory_rows if r["country"] == country]
        pm = [p for p in pathway_metrics if p["country"] == country]
        true_pm = [p for p in pm if p["entity_type"] == "true_pathway"]
        n_true = len(true_pm)
        linked = sum(1 for p in true_pm if p["pathway_source_linked"])
        ing = sum(1 for p in true_pm if p["pathway_relevant_document_ingested"])
        ext = sum(1 for p in true_pm if p["pathway_relevant_document_extracted"])
        legacy = sum(1 for p in true_pm if p["legacy_source_wide_ingested_documents"] > 0)
        rows.append(
            {
                "country": country,
                "scope": "eu_framework" if country == "EU" else "national",
                "entities_total": len(inv),
                "true_pathways": n_true,
                "information_hubs": sum(1 for r in inv if r["entity_type"] == "information_hub"),
                "cross_cutting_guidance": sum(
                    1 for r in inv if r["entity_type"] == "cross_cutting_guidance"
                ),
                "unknown_candidates": sum(1 for r in inv if r["entity_type"] == "unknown_candidate"),
                "approved_sources_in_country": approved_by_country.get(country, 0),
                "country_has_approved_source": approved_by_country.get(country, 0) > 0,
                "pathway_source_linked": linked,
                "pathway_source_linked_rate": round(linked / n_true, 4) if n_true else 0.0,
                "pathway_relevant_document_ingested": ing,
                "pathway_relevant_document_ingested_rate": round(ing / n_true, 4) if n_true else 0.0,
                "pathway_relevant_document_extracted": ext,
                "pathway_relevant_document_extracted_rate": round(ext / n_true, 4) if n_true else 0.0,
                "legacy_source_wide_ingested_pathways": legacy,
                "rate_denominator": "true_pathway entities in country",
            }
        )
    return rows


def metrics_totals(country_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """National totals exclude the EU framework bucket (reported separately)."""
    national = [r for r in country_rows if r["scope"] == "national"]
    eu = [r for r in country_rows if r["scope"] == "eu_framework"]

    def agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
        n = sum(r["true_pathways"] for r in rows)
        return {
            "entities_total": sum(r["entities_total"] for r in rows),
            "true_pathways": n,
            "countries_with_approved_source": sum(1 for r in rows if r["country_has_approved_source"]),
            "pathway_source_linked": sum(r["pathway_source_linked"] for r in rows),
            "pathway_relevant_document_ingested": sum(
                r["pathway_relevant_document_ingested"] for r in rows
            ),
            "pathway_relevant_document_extracted": sum(
                r["pathway_relevant_document_extracted"] for r in rows
            ),
            "legacy_source_wide_ingested_pathways": sum(
                r["legacy_source_wide_ingested_pathways"] for r in rows
            ),
        }

    return {
        "national": agg(national),
        "eu_framework": agg(eu),
        "definitions": {
            "country_has_approved_source": (
                "at least one approved Source whose jurisdiction is that country"
            ),
            "pathway_source_linked": (
                "an approved Source is explicitly linked to the true pathway "
                "(inventory source_ids or an owner-approved 6H decision)"
            ),
            "pathway_relevant_document_ingested": (
                "a stored document whose URL matches the pathway's own URL tokens/official "
                "URL scope (same-country guard); shared multi-pathway pages count only here, "
                "never as single-pathway evidence"
            ),
            "pathway_relevant_document_extracted": (
                "as above and the current version has a successful extraction"
            ),
            "eu": "EU framework entities/sources are excluded from national totals",
        },
    }
