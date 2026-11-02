"""Load extraction views from stored JSON when Postgres is unavailable."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from lifepilot.coverage.inventory import ExtractionView, is_thin_content
from lifepilot.crawl.storage import data_root

_VERSION_RE = re.compile(r"^v(\d+)\.(.+)\.json$", re.IGNORECASE)


def _analyze_blocks(
    payload: dict[str, Any],
) -> tuple[list[str], list[str], list[str], int, int]:
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
    return headings, paragraphs, list_items, text_chars, len(blocks)


def _latest_extraction(doc_dir: Path) -> tuple[Path, int, str] | None:
    best: tuple[int, Path, str] | None = None
    if not doc_dir.is_dir():
        return None
    for path in doc_dir.glob("v*.json"):
        m = _VERSION_RE.match(path.name)
        if not m:
            continue
        ver = int(m.group(1))
        extractor = m.group(2)
        if best is None or ver > best[0]:
            best = (ver, path, extractor)
    if best is None:
        return None
    return best[1], best[0], best[2]


def load_corpus_from_disk(
    entities: list[Any],
    *,
    data_dir: Path | None = None,
) -> list[ExtractionView]:
    """Build ExtractionView rows from inventory document IDs + extracted JSON."""
    root = data_root(data_dir)
    extracted_root = root / "extracted" / "documents"
    views: list[ExtractionView] = []
    seen_docs: set[int] = set()

    for ent in entities:
        country = getattr(ent, "country", "") or ""
        source_ids = list(getattr(ent, "source_ids", []) or [])
        source_id = source_ids[0] if source_ids else 0
        for doc_id in getattr(ent, "document_ids", []) or []:
            if doc_id in seen_docs:
                continue
            seen_docs.add(int(doc_id))
            doc_dir = extracted_root / str(doc_id)
            latest = _latest_extraction(doc_dir)
            if latest is None:
                continue
            path, version_number, extractor = latest
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            headings, paragraphs, list_items, text_chars, block_count = _analyze_blocks(
                payload
            )
            para_chars = sum(len(p) for p in paragraphs)
            thin = is_thin_content(
                text_chars=text_chars,
                block_count=block_count,
                paragraph_chars=para_chars,
            )
            title = str(payload.get("title") or "").strip()
            source_url = str(payload.get("source_url") or "")
            version_id = int(payload.get("document_version_id") or version_number)
            # Infer language mismatch candidates from URL + any metadata
            ver_lang = None
            meta = payload.get("metadata") or {}
            if isinstance(meta, dict) and meta.get("language"):
                ver_lang = str(meta.get("language"))
            # NL PDFs under /en/ paths are often Dutch-language content
            if extractor.startswith("pdf") and "/en/" in source_url.lower():
                body = " ".join([title] + paragraphs[:8] + headings[:8]).lower()
                dutch_markers = (
                    "nederlander",
                    "naturalisatie",
                    "moet u",
                    "u moet",
                    "aanvraag",
                    "verblijf",
                    "nationaliteit",
                    "publicatie",
                )
                if any(m in body for m in dutch_markers):
                    ver_lang = "nl"

            views.append(
                ExtractionView(
                    document_id=int(doc_id),
                    version_id=version_id,
                    source_id=int(source_id),
                    country=str(country).upper(),
                    extractor=extractor,
                    title=title,
                    canonical_url=source_url,
                    source_url=source_url,
                    discovery_status="discovered",
                    doc_language="en" if "/en/" in source_url.lower() else None,
                    version_language=ver_lang,
                    http_status=200,
                    storage_path=str(
                        path.relative_to(root).as_posix()
                        if path.is_relative_to(root)
                        else path
                    ),
                    headings=headings,
                    paragraphs=paragraphs,
                    list_items=list_items,
                    text_chars=text_chars,
                    block_count=block_count,
                    is_thin=thin,
                    is_blocked_evidence=source_id in {87, 92},
                )
            )
    return views
