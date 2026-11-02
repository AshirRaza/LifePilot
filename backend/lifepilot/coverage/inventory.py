"""Build pathway inventory from approved sources + stored extractions (read-only)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifepilot.coverage.schema import PathwayRecord, PathwayStatus
from lifepilot.coverage.taxonomy import (
    AUDIT_COUNTRIES,
    COUNTRY_PARENT_SLOTS,
    THIN_MIN_BLOCKS,
    THIN_MIN_PARAGRAPH_CHARS,
    THIN_MIN_TEXT_CHARS,
    PathwaySeed,
    load_pathway_seeds,
)
from lifepilot.crawl.storage import data_root, safe_read_bytes

_WS = re.compile(r"\s+")


@dataclass
class ExtractionView:
    document_id: int
    version_id: int
    source_id: int
    country: str
    extractor: str
    title: str
    canonical_url: str
    source_url: str
    discovery_status: str
    doc_language: str | None
    version_language: str | None
    http_status: int | None
    storage_path: str | None
    headings: list[str] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    list_items: list[str] = field(default_factory=list)
    text_chars: int = 0
    block_count: int = 0
    is_thin: bool = True
    is_blocked_evidence: bool = False

    def searchable_text(self) -> str:
        parts = [self.title, *self.headings, *self.paragraphs, *self.list_items]
        return _WS.sub(" ", " ".join(p for p in parts if p)).lower()


@dataclass
class InventoryBundle:
    pathways: list[PathwayRecord]
    extractions: list[ExtractionView]
    sources_by_country: dict[str, list[dict[str, Any]]]
    country_notes: dict[str, list[str]]
    oos_leads: list[str]
    frontier_pending_by_country: dict[str, int]


def _load_extraction_payload(root: Path, storage_path: str | None) -> dict[str, Any]:
    if not storage_path:
        return {}
    try:
        return json.loads(safe_read_bytes(root, storage_path).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _analyze_blocks(payload: dict[str, Any]) -> tuple[list[str], list[str], list[str], int, int]:
    headings: list[str] = []
    paragraphs: list[str] = []
    list_items: list[str] = []
    text_chars = 0
    blocks = payload.get("blocks") or []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "heading":
            t = str(block.get("text") or "").strip()
            if t:
                headings.append(t)
                text_chars += len(t)
        elif btype == "paragraph":
            t = str(block.get("text") or "").strip()
            if t:
                paragraphs.append(t)
                text_chars += len(t)
        elif btype == "list":
            for item in block.get("items") or []:
                s = str(item).strip()
                if s:
                    list_items.append(s)
                    text_chars += len(s)
        elif btype == "table":
            for row in block.get("rows") or []:
                for cell in row:
                    s = str(cell).strip()
                    if s:
                        text_chars += len(s)
    return headings, paragraphs, list_items, text_chars, len(blocks)


def is_thin_content(
    *,
    text_chars: int,
    block_count: int,
    paragraph_chars: int,
) -> bool:
    """Navigation/hub heuristics — thin pages cannot yield COMPLETE."""
    if text_chars < THIN_MIN_TEXT_CHARS:
        return True
    if block_count < THIN_MIN_BLOCKS:
        return True
    if paragraph_chars < THIN_MIN_PARAGRAPH_CHARS:
        return True
    return False


def detect_nl_language_mismatch(view: ExtractionView) -> bool:
    """Flag EN URL/path with version language nl (NL naturalisation PDFs)."""
    url = (view.canonical_url or view.source_url or "").lower()
    ver_lang = (view.version_language or "").lower()
    if ver_lang != "nl":
        return False
    if "/en/" in url or url.endswith(".pdf") and "/en/" in url:
        return True
    doc_lang = (view.doc_language or "").lower()
    return doc_lang == "en" and ver_lang == "nl"


def detect_au_gte_redirect(views: Iterable[ExtractionView]) -> list[str]:
    notes: list[str] = []
    for v in views:
        blob = " ".join(
            [
                v.canonical_url or "",
                v.source_url or "",
                v.title or "",
            ]
        ).lower()
        if "genuine-temporary-entrant" in blob or "genuine temporary entrant" in blob:
            if "genuine-student" in blob or "genuine student" in blob:
                notes.append(
                    f"doc {v.document_id}: GTE and Genuine Student both present in stored URLs/title"
                )
            else:
                notes.append(
                    f"doc {v.document_id}: GTE URL/title present; Genuine Student redirect "
                    "provenance unevaluated (no final_url field stored)"
                )
    return notes


def _blocked_evidence(discovery_status: str, source_id: int) -> bool:
    if discovery_status == "rejected":
        return True
    # Make-it-in-Germany sources are known blocked/rejected.
    if source_id in {87, 92}:
        return True
    return False


def load_corpus(session: Session, *, data_dir: Path | None = None) -> list[ExtractionView]:
    root = data_root(data_dir)
    rows = session.execute(
        text(
            """
            SELECT
              d.id AS document_id,
              dv.id AS version_id,
              s.id AS source_id,
              j.code AS country,
              de.extractor_name AS extractor,
              coalesce(de.title, d.title, '') AS title,
              d.canonical_url,
              dv.source_url,
              d.discovery_status,
              d.language AS doc_language,
              dv.language AS version_language,
              dv.http_status,
              de.storage_path
            FROM document_extractions de
            JOIN document_versions dv ON dv.id = de.document_version_id
            JOIN documents d ON d.id = dv.document_id
            JOIN sources s ON s.id = d.primary_source_id
            JOIN jurisdictions j ON j.id = s.jurisdiction_id
            WHERE s.review_status = 'approved'
              AND de.status = 'success'
              AND dv.is_current IS TRUE
            ORDER BY j.code, d.id
            """
        )
    ).mappings().all()

    views: list[ExtractionView] = []
    for row in rows:
        payload = _load_extraction_payload(root, row["storage_path"])
        headings, paragraphs, list_items, text_chars, block_count = _analyze_blocks(payload)
        title = (row["title"] or payload.get("title") or "").strip()
        para_chars = sum(len(p) for p in paragraphs)
        thin = is_thin_content(
            text_chars=text_chars,
            block_count=block_count,
            paragraph_chars=para_chars,
        )
        views.append(
            ExtractionView(
                document_id=int(row["document_id"]),
                version_id=int(row["version_id"]),
                source_id=int(row["source_id"]),
                country=str(row["country"]).upper(),
                extractor=str(row["extractor"]),
                title=title,
                canonical_url=str(row["canonical_url"] or ""),
                source_url=str(row["source_url"] or ""),
                discovery_status=str(row["discovery_status"] or ""),
                doc_language=row["doc_language"],
                version_language=row["version_language"],
                http_status=row["http_status"],
                storage_path=row["storage_path"],
                headings=headings,
                paragraphs=paragraphs,
                list_items=list_items,
                text_chars=text_chars,
                block_count=block_count,
                is_thin=thin,
                is_blocked_evidence=_blocked_evidence(
                    str(row["discovery_status"] or ""), int(row["source_id"])
                ),
            )
        )
    return views


def load_approved_sources(session: Session) -> dict[str, list[dict[str, Any]]]:
    rows = session.execute(
        text(
            """
            SELECT
              s.id,
              s.name,
              j.code AS country,
              a.name AS authority,
              (SELECT count(*) FROM documents d WHERE d.primary_source_id = s.id) AS doc_count
            FROM sources s
            JOIN jurisdictions j ON j.id = s.jurisdiction_id
            JOIN authorities a ON a.id = s.authority_id
            WHERE s.review_status = 'approved'
            ORDER BY j.code, s.id
            """
        )
    ).mappings().all()
    out: dict[str, list[dict[str, Any]]] = {c: [] for c in AUDIT_COUNTRIES}
    for row in rows:
        country = str(row["country"]).upper()
        out.setdefault(country, []).append(dict(row))
    return out


def load_frontier_pending(session: Session) -> dict[str, int]:
    rows = session.execute(
        text(
            """
            SELECT j.code AS country, count(*) AS n
            FROM discovery_frontier fr
            JOIN sources s ON s.id = fr.source_id
            JOIN jurisdictions j ON j.id = s.jurisdiction_id
            WHERE fr.status = 'pending'
            GROUP BY j.code
            """
        )
    ).mappings().all()
    return {str(r["country"]).upper(): int(r["n"]) for r in rows}


def load_oos_leads(repo_root: Path | None = None) -> list[str]:
    root = repo_root or Path(__file__).resolve().parents[3]
    path = root / "data" / "runs" / "phase_6d8_discovery_validation.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    urls: list[str] = []

    # Prefer curated 6D.8 proposals; top-up from PageSchema OOS lists (no invented URLs).
    for item in data.get("coverage_gap_proposals_sample") or []:
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str) and url.startswith("http"):
                urls.append(url)

    target = int(data.get("coverage_gap_proposals_count") or 0)
    if target <= 0:
        target = 54

    if len({u for u in urls}) < target:
        for case in data.get("per_source") or []:
            if not isinstance(case, dict):
                continue
            for url in case.get("pageschema_out_of_scope") or []:
                if isinstance(url, str) and url.startswith("http"):
                    urls.append(url)

    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
            if len(out) >= target:
                break
    return out


def _matches_seed(seed: PathwaySeed, view: ExtractionView) -> bool:
    if view.source_id not in seed.source_ids and seed.source_ids:
        # Allow match only within seeded sources.
        return False
    title_l = view.title.lower()
    url_l = f"{view.canonical_url} {view.source_url}".lower()
    headings_l = " ".join(view.headings[:20]).lower()
    for pat in seed.match_urls:
        if pat and pat in url_l:
            return True
    for pat in seed.match_titles:
        if pat and pat in title_l:
            return True
    for pat in seed.match_headings:
        if pat and pat in headings_l:
            return True
    # Source-level seed with no document-level match: attach seed docs for that source only
    # when the seed has empty/generic patterns and this is the only pathway for the source.
    return False


def _source_level_attach(seed: PathwaySeed, view: ExtractionView) -> bool:
    """Attach all source docs when seed is source-scoped and patterns don't exclude."""
    if view.source_id not in seed.source_ids:
        return False
    # Prefer specific title/url match when patterns exist; otherwise attach all source docs.
    if seed.match_titles or seed.match_urls or seed.match_headings:
        return _matches_seed(seed, view)
    return True


def build_inventory(
    session: Session,
    *,
    data_dir: Path | None = None,
    seeds: tuple[PathwaySeed, ...] | None = None,
    repo_root: Path | None = None,
) -> InventoryBundle:
    seed_list = list(seeds if seeds is not None else load_pathway_seeds())
    extractions = load_corpus(session, data_dir=data_dir)
    sources_by_country = load_approved_sources(session)
    frontier_pending = load_frontier_pending(session)
    oos_leads = load_oos_leads(repo_root)
    by_source: dict[int, list[ExtractionView]] = {}
    for ex in extractions:
        by_source.setdefault(ex.source_id, []).append(ex)

    pathways: list[PathwayRecord] = []
    covered_slots: dict[str, set[str]] = {c: set() for c in AUDIT_COUNTRIES}
    country_notes: dict[str, list[str]] = {c: [] for c in AUDIT_COUNTRIES}

    # Special-case notes
    if frontier_pending.get("NZ"):
        country_notes["NZ"].append(
            f"{frontier_pending['NZ']} pending discovery_frontier rows outstanding"
        )
    if oos_leads:
        country_notes["AU"].append(
            f"{len(oos_leads)} OOS URLs from phase_6d8_discovery_validation.json retained as leads only"
        )
    country_notes["DE"].append(
        "Sources 87/92 Make-it-in-Germany rejected/blocked (WAF); BAMF/AA usable"
    )
    country_notes["AU"].append(
        "Sources 11024/11025/11027 seed-limited relative to full program pages"
    )

    for seed in seed_list:
        linked: list[ExtractionView] = []
        for sid in seed.source_ids:
            for view in by_source.get(sid, []):
                if _source_level_attach(seed, view):
                    linked.append(view)
        if not linked:
            for sid in seed.source_ids:
                # Only auto-attach all docs when this source maps to exactly one seed
                seeds_for_source = [s for s in seed_list if sid in s.source_ids]
                if (
                    len(seeds_for_source) == 1
                    and seeds_for_source[0].pathway_id == seed.pathway_id
                ):
                    linked.extend(by_source.get(sid, []))

        usable = [v for v in linked if not v.is_blocked_evidence]
        non_thin = [v for v in usable if not v.is_thin]
        notes = list(seed.notes)
        for v in linked:
            if detect_nl_language_mismatch(v):
                notes.append(
                    f"doc {v.document_id}: language metadata mismatch "
                    f"(doc_lang={v.doc_language!r}, version_lang={v.version_language!r}, "
                    f"url={v.canonical_url})"
                )
        if seed.pathway_id == "au_student_500":
            notes.extend(detect_au_gte_redirect(linked))

        if linked and non_thin:
            status = PathwayStatus.VERIFIED
            evidence_thin = False
        elif linked or seed.source_ids:
            status = PathwayStatus.CANDIDATE
            evidence_thin = True
            if not linked:
                notes.append("Named by approved source/seed but no usable extraction linked")
            elif not usable:
                notes.append("Only blocked/rejected documents linked")
            else:
                notes.append("Linked extractions are thin/hub-like")
        else:
            status = PathwayStatus.CANDIDATE
            evidence_thin = True
            notes.append("Seed present without linked corpus evidence")

        pathways.append(
            PathwayRecord(
                pathway_id=seed.pathway_id,
                country=seed.country,
                parent_category=seed.parent_category,
                name=seed.name,
                status=status,
                source_ids=list(seed.source_ids),
                document_ids=sorted({v.document_id for v in linked}),
                evidence_thin=evidence_thin,
                financial_applicable=seed.financial_applicable,
                notes=notes,
                provenance={
                    "label": seed.provenance,
                    "source_ids": list(seed.source_ids),
                    "match_titles": list(seed.match_titles),
                },
            )
        )
        covered_slots.setdefault(seed.country, set()).add(seed.parent_category)

    # Parent slots with no grounded pathway → unknown_or_not_inventoried
    for country in AUDIT_COUNTRIES:
        for slot in COUNTRY_PARENT_SLOTS.get(country, ()):
            if slot in covered_slots.get(country, set()):
                continue
            pathways.append(
                PathwayRecord(
                    pathway_id=f"{country.lower()}_unknown_{slot}",
                    country=country,
                    parent_category=slot,
                    name=f"{country} {slot} (not inventoried)",
                    status=PathwayStatus.UNKNOWN_OR_NOT_INVENTORIED,
                    source_ids=[],
                    document_ids=[],
                    evidence_thin=True,
                    financial_applicable=True,
                    notes=["No approved source/extraction grounding for this parent category"],
                    provenance={},
                )
            )

    pathways.sort(key=lambda p: (AUDIT_COUNTRIES.index(p.country) if p.country in AUDIT_COUNTRIES else 99, p.pathway_id))
    return InventoryBundle(
        pathways=pathways,
        extractions=extractions,
        sources_by_country=sources_by_country,
        country_notes=country_notes,
        oos_leads=oos_leads,
        frontier_pending_by_country=frontier_pending,
    )
