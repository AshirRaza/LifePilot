"""Build Phase 6G pathway inventory enriched with live corpus stats."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from lifepilot.coverage.pathway_inventory.inventory_data import build_static_inventory
from lifepilot.db.session import get_session_factory


def _source_doc_stats() -> dict[int, dict[str, Any]]:
    session = get_session_factory()()
    try:
        rows = session.execute(
            text(
                """
                SELECT
                  src.id AS source_id,
                  src.review_status,
                  COALESCE(j.country_code, 'UNKNOWN') AS country_code,
                  count(DISTINCT d.id) AS documents,
                  max(v.retrieved_at) AS last_fetch,
                  max(e.created_at) AS last_extraction
                FROM sources src
                LEFT JOIN jurisdictions j ON j.id = src.jurisdiction_id
                LEFT JOIN documents d ON d.primary_source_id = src.id
                LEFT JOIN document_versions v ON v.document_id = d.id AND v.is_current
                LEFT JOIN document_extractions e
                  ON e.document_version_id = v.id AND e.status = 'success'
                GROUP BY src.id, src.review_status, j.country_code
                """
            )
        ).mappings().all()
        return {int(r["source_id"]): dict(r) for r in rows}
    finally:
        session.close()


def _url_document_counts(urls: list[str]) -> dict[str, int]:
    """Best-effort exact URL hits only (avoid per-URL LIKE scans)."""
    clean = [u for u in urls if u]
    if not clean:
        return {}
    session = get_session_factory()()
    try:
        rows = session.execute(
            text(
                """
                SELECT d.canonical_url AS url, count(DISTINCT d.id) AS n
                FROM documents d
                WHERE d.canonical_url = ANY(:urls)
                GROUP BY d.canonical_url
                """
            ),
            {"urls": clean},
        ).mappings().all()
        return {str(r["url"]): int(r["n"]) for r in rows}
    finally:
        session.close()


def build_inventory_rows() -> list[dict[str, Any]]:
    static = build_static_inventory()
    stats = _source_doc_stats()
    urls = [r["official_url"] for r in static if r.get("official_url")]
    url_counts = _url_document_counts(urls)

    rows: list[dict[str, Any]] = []
    for raw in static:
        source_ids = list(raw.get("source_ids") or [])
        docs = 0
        last_fetch = None
        last_ext = None
        approved_source = False
        for sid in source_ids:
            st = stats.get(sid)
            if not st:
                continue
            docs += int(st.get("documents") or 0)
            if st.get("review_status") == "approved":
                approved_source = True
            lf = st.get("last_fetch")
            le = st.get("last_extraction")
            if lf and (last_fetch is None or lf > last_fetch):
                last_fetch = lf
            if le and (last_ext is None or le > last_ext):
                last_ext = le
        url = raw.get("official_url")
        if url and url_counts.get(url, 0) > docs:
            docs = url_counts[url]

        entity = raw["entity_type"]
        rows.append(
            {
                **raw,
                "approved_source_present": approved_source,
                "ingested_documents": docs,
                "last_successful_fetch": last_fetch.isoformat() if last_fetch else "",
                "last_successful_extraction": last_ext.isoformat() if last_ext else "",
                "source_approval_status": (
                    "approved_source_linked"
                    if approved_source
                    else (
                        "candidate_or_missing"
                        if entity != "unknown_candidate"
                        else "unknown"
                    )
                ),
                "is_true_pathway": entity == "true_pathway",
            }
        )
    return rows


def inventory_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    true_rows = [r for r in rows if r["entity_type"] == "true_pathway"]
    hubs = [r for r in rows if r["entity_type"] == "information_hub"]
    unknown = [r for r in rows if r["entity_type"] == "unknown_candidate"]
    by_country: dict[str, dict[str, int]] = {}
    for r in rows:
        c = r["country"]
        bucket = by_country.setdefault(
            c,
            {
                "true_pathways": 0,
                "hubs": 0,
                "unknown_candidates": 0,
                "cross_cutting": 0,
                "with_approved_source": 0,
                "with_ingested_docs": 0,
            },
        )
        if r["entity_type"] == "true_pathway":
            bucket["true_pathways"] += 1
            if r["approved_source_present"]:
                bucket["with_approved_source"] += 1
            if r["ingested_documents"] > 0:
                bucket["with_ingested_docs"] += 1
        elif r["entity_type"] == "information_hub":
            bucket["hubs"] += 1
        elif r["entity_type"] == "unknown_candidate":
            bucket["unknown_candidates"] += 1
        elif r["entity_type"] == "cross_cutting_guidance":
            bucket["cross_cutting"] += 1

    return {
        "total_entities": len(rows),
        "true_pathways": len(true_rows),
        "information_hubs": len(hubs),
        "unknown_candidates": len(unknown),
        "true_pathways_with_approved_source": sum(
            1 for r in true_rows if r["approved_source_present"]
        ),
        "true_pathways_with_ingested_docs": sum(
            1 for r in true_rows if r["ingested_documents"] > 0
        ),
        "by_country": by_country,
    }
