"""Structured text extraction from stored PDF document versions (no OCR)."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import pymupdf

from lifepilot.extract.schema import (
    ContentBlock,
    StructuredDocument,
    normalize_text,
    normalized_content_sha256,
)

EXTRACTOR_NAME = "pdf_structured"
EXTRACTOR_VERSION = "1.1.0"

# Fraction of text-block area that must overlap a table bbox to skip the text.
_TABLE_OVERLAP_RATIO = 0.55
_BULLET_PREFIXES = ("•", "●", "○", "▪", "▫", "·", "∙", "-", "–", "—", "*")
_PUA_RE = re.compile(r"[\uf000-\uf8ff]")
_CHECKBOX_OPTIONS = frozenset(
    {"yes", "no", "male", "female", "n/a", "na", "true", "false"}
)
_NUMBERED_HEADING_RE = re.compile(r"^\d{1,2}[.)]\s+\S.{2,}$")
_SECTION_NUMBER_RE = re.compile(r"^\d{1,2}[.)]?$")


def _rect_overlap_ratio(inner: pymupdf.Rect, outer: pymupdf.Rect) -> float:
    inter = pymupdf.Rect(inner)
    inter.intersect(outer)
    if inter.is_empty or inner.get_area() <= 0:
        return 0.0
    return inter.get_area() / inner.get_area()


def _overlaps_any_table(
    bbox: tuple[float, float, float, float], tables: list[pymupdf.Rect]
) -> bool:
    rect = pymupdf.Rect(bbox)
    return any(_rect_overlap_ratio(rect, t) >= _TABLE_OVERLAP_RATIO for t in tables)


def _collect_table_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    rects: list[pymupdf.Rect] = []
    finder = getattr(page, "find_tables", None)
    if finder is None:
        return rects
    try:
        found = finder()
    except Exception:  # noqa: BLE001 — table finder is best-effort
        return rects
    tables = getattr(found, "tables", None) or []
    for table in tables:
        bbox = getattr(table, "bbox", None)
        if bbox is not None:
            rects.append(pymupdf.Rect(bbox))
    return rects


def _extract_tables(page: pymupdf.Page, page_num: int) -> list[ContentBlock]:
    blocks: list[ContentBlock] = []
    finder = getattr(page, "find_tables", None)
    if finder is None:
        return blocks
    try:
        found = finder()
    except Exception:  # noqa: BLE001
        return blocks
    for table in getattr(found, "tables", None) or []:
        try:
            raw_rows = table.extract() or []
        except Exception:  # noqa: BLE001
            continue
        rows: list[tuple[str, ...]] = []
        for row in raw_rows:
            cells = tuple(normalize_text(c if c is not None else "") for c in row)
            if any(cells):
                rows.append(cells)
        if not rows:
            continue
        headers: tuple[str, ...] = ()
        body = rows
        if len(rows) >= 2 and any(rows[0]):
            headers = rows[0]
            body = rows[1:]
        blocks.append(
            ContentBlock(
                type="table",
                headers=headers,
                rows=tuple(body),
                page=page_num,
            )
        )
    return blocks


def _span_sizes(page_dict: dict[str, Any]) -> list[float]:
    sizes: list[float] = []
    for block in page_dict.get("blocks") or []:
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or []:
            for span in line.get("spans") or []:
                size = float(span.get("size") or 0)
                text = normalize_text(span.get("text"))
                if size > 0 and text:
                    sizes.append(size)
    return sizes


def _body_font_size(sizes: list[float]) -> float:
    """Estimate body font size without letting sparse titles dominate.

    Prefer a lower-percentile size, then fall back to the most common size at or
    below that percentile. This keeps form titles larger than body text even
    when a page has many large heading-like spans.
    """
    if not sizes:
        return 0.0
    rounded = [round(size, 1) for size in sizes]
    ordered = sorted(rounded)
    idx = max(0, min(len(ordered) - 1, int(len(ordered) * 0.25)))
    pivot = ordered[idx]
    at_or_below = [size for size in rounded if size <= pivot + 0.05]
    if at_or_below:
        return float(Counter(at_or_below).most_common(1)[0][0])
    return float(Counter(rounded).most_common(1)[0][0])


def _heading_level(size: float, body_size: float) -> int | None:
    if body_size <= 0:
        return None
    ratio = size / body_size
    if ratio >= 1.55:
        return 1
    if ratio >= 1.35:
        return 2
    if ratio >= 1.15:
        return 3
    # Absolute gap helps when body estimate is slightly high.
    if size >= body_size + 2.0:
        return 3
    return None


def _line_text(line: dict[str, Any]) -> tuple[str, float]:
    parts: list[str] = []
    max_size = 0.0
    for span in line.get("spans") or []:
        text = span.get("text") or ""
        if text:
            parts.append(text)
        max_size = max(max_size, float(span.get("size") or 0))
    return normalize_text("".join(parts)), max_size


def _strip_pua(text: str) -> str:
    return normalize_text(_PUA_RE.sub(" ", text))


def _is_junk_heading_text(text: str) -> bool:
    """True for checkbox glyphs, lone section numbers, and PUA option stubs."""
    t = normalize_text(text)
    if not t:
        return True
    if t.isdigit() or _SECTION_NUMBER_RE.fullmatch(t):
        return True
    if not re.search(r"[A-Za-z]", t):
        return True
    cleaned = _strip_pua(t)
    if not cleaned:
        return True
    # Checkbox / symbol fonts (Private Use Area) are never genuine headings,
    # including long option lists that begin with PUA glyphs.
    if _PUA_RE.search(t):
        return True
    return False


def _looks_like_numbered_heading(text: str) -> bool:
    """Preserve genuine headings like '2. Requirements' (not lone '2')."""
    t = normalize_text(text)
    return bool(_NUMBERED_HEADING_RE.match(t)) and not _is_junk_heading_text(t)


def _paragraph_from_rejected_heading(text: str) -> str:
    cleaned = _strip_pua(text)
    return cleaned or normalize_text(text)


def _is_list_line(text: str) -> tuple[bool, bool, str]:
    """Return (is_list, ordered, item_text)."""
    if not text:
        return False, False, ""
    if _looks_like_numbered_heading(text):
        return False, False, text
    if text[0].isdigit():
        for i, ch in enumerate(text):
            if ch in {".", ")"} and i > 0 and text[:i].isdigit():
                rest = text[i + 1 :].strip()
                if rest:
                    return True, True, rest
                break
    for prefix in _BULLET_PREFIXES:
        if text.startswith(prefix):
            rest = text[len(prefix) :].strip()
            if rest:
                return True, False, rest
    # Checkbox glyph used as a bullet.
    if _PUA_RE.match(text):
        rest = _strip_pua(text)
        if rest:
            return True, False, rest
    return False, False, text


def _should_merge_heading_texts(first: str, second: str) -> bool:
    if not first or not second:
        return False
    if _is_junk_heading_text(first) or _is_junk_heading_text(second):
        return False
    if _looks_like_numbered_heading(second) or _SECTION_NUMBER_RE.fullmatch(second):
        return False
    if first.endswith((".", "!", "?", ":")):
        return False
    if first.endswith((",", ";", "-", "–", "—", "/", "'", "’", "‘")):
        return True
    # Continuation mid-phrase (common for wrapped form titles).
    if second[0].islower() or second[0] in {"'", "’", "‘", '"'}:
        return True
    return False


def _merge_consecutive_headings(blocks: list[ContentBlock]) -> list[ContentBlock]:
    """Merge adjacent same-page heading fragments that clearly form one title."""
    if not blocks:
        return blocks
    merged: list[ContentBlock] = [blocks[0]]
    for block in blocks[1:]:
        prev = merged[-1]
        if (
            prev.type == "heading"
            and block.type == "heading"
            and prev.page is not None
            and prev.page == block.page
            and prev.level == block.level
            and _should_merge_heading_texts(prev.text or "", block.text or "")
        ):
            merged[-1] = ContentBlock(
                type="heading",
                level=prev.level,
                text=normalize_text(f"{prev.text} {block.text}"),
                page=prev.page,
            )
        else:
            merged.append(block)
    return merged


def _emit_heading_or_paragraph(
    out: list[ContentBlock],
    *,
    text: str,
    level: int,
    page_num: int,
    flush_list,
) -> None:
    flush_list()
    if _is_junk_heading_text(text):
        para = _paragraph_from_rejected_heading(text)
        if para:
            out.append(ContentBlock(type="paragraph", text=para, page=page_num))
        return
    out.append(ContentBlock(type="heading", level=level, text=text, page=page_num))


def _extract_text_blocks(
    page: pymupdf.Page,
    page_num: int,
    table_rects: list[pymupdf.Rect],
) -> list[ContentBlock]:
    page_dict = page.get_text("dict")
    sizes = _span_sizes(page_dict)
    body_size = _body_font_size(sizes)

    out: list[ContentBlock] = []
    pending_list: list[str] = []
    pending_ordered: bool | None = None

    def flush_list() -> None:
        nonlocal pending_list, pending_ordered
        if pending_list:
            out.append(
                ContentBlock(
                    type="list",
                    ordered=bool(pending_ordered),
                    items=tuple(pending_list),
                    page=page_num,
                )
            )
        pending_list = []
        pending_ordered = None

    for block in page_dict.get("blocks") or []:
        if block.get("type") != 0:
            continue
        bbox = block.get("bbox")
        if bbox and table_rects and _overlaps_any_table(tuple(bbox), table_rects):
            continue

        lines_text: list[tuple[str, float]] = []
        for line in block.get("lines") or []:
            text, size = _line_text(line)
            if text:
                lines_text.append((text, size))
        if not lines_text:
            continue

        # Single-line block: may be heading or list item.
        if len(lines_text) == 1:
            text, size = lines_text[0]
            level = _heading_level(size, body_size)
            is_list, ordered, item = _is_list_line(text)
            if _looks_like_numbered_heading(text):
                _emit_heading_or_paragraph(
                    out,
                    text=text,
                    level=level or 2,
                    page_num=page_num,
                    flush_list=flush_list,
                )
                continue
            if level is not None and not is_list:
                _emit_heading_or_paragraph(
                    out, text=text, level=level, page_num=page_num, flush_list=flush_list
                )
                continue
            if is_list:
                if pending_ordered is not None and pending_ordered != ordered:
                    flush_list()
                pending_ordered = ordered
                pending_list.append(item)
                continue
            flush_list()
            out.append(ContentBlock(type="paragraph", text=text, page=page_num))
            continue

        flush_list()
        first_size = lines_text[0][1]
        first_level = _heading_level(first_size, body_size)
        all_headingish = first_level is not None and all(
            (
                _heading_level(size, body_size) is not None
                or abs(size - first_size) <= 0.6
            )
            and not _is_junk_heading_text(text)
            for text, size in lines_text
        )
        if all_headingish and first_level is not None:
            merged = normalize_text(" ".join(t for t, _ in lines_text))
            _emit_heading_or_paragraph(
                out,
                text=merged,
                level=first_level,
                page_num=page_num,
                flush_list=flush_list,
            )
            continue

        paragraph = normalize_text(" ".join(t for t, _ in lines_text))
        if paragraph:
            # Wrapped title: same large font across lines → one heading.
            sizes_close = all(
                abs(size - first_size) <= 0.6 for _, size in lines_text
            )
            if (
                first_level is not None
                and sizes_close
                and all(not _is_junk_heading_text(text) for text, _ in lines_text)
            ):
                _emit_heading_or_paragraph(
                    out,
                    text=paragraph,
                    level=first_level,
                    page_num=page_num,
                    flush_list=flush_list,
                )
            elif first_level is not None and len(lines_text) <= 2:
                _emit_heading_or_paragraph(
                    out,
                    text=lines_text[0][0],
                    level=first_level,
                    page_num=page_num,
                    flush_list=flush_list,
                )
                rest = normalize_text(" ".join(t for t, _ in lines_text[1:]))
                if rest:
                    out.append(
                        ContentBlock(type="paragraph", text=rest, page=page_num)
                    )
            else:
                out.append(
                    ContentBlock(type="paragraph", text=paragraph, page=page_num)
                )

    flush_list()
    return out


def _block_plain_text(block: ContentBlock) -> str:
    if block.type in {"heading", "paragraph"}:
        return block.text or ""
    if block.type == "list":
        return " ".join(block.items or ())
    if block.type == "table":
        parts: list[str] = []
        if block.caption:
            parts.append(block.caption)
        parts.extend(block.headers or ())
        for row in block.rows or ():
            parts.extend(row)
        return " ".join(parts)
    return ""


def _normalize_for_dedupe(text: str) -> str:
    cleaned = _strip_pua(text).lower()
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    return normalize_text(cleaned)


def _extract_form_labels(
    page: pymupdf.Page,
    page_num: int,
    existing_blocks: list[ContentBlock],
) -> list[ContentBlock]:
    """Surface AcroForm labels not already present in page text."""
    blocks: list[ContentBlock] = []
    widgets = page.widgets()
    if not widgets:
        return blocks

    page_corpus = _normalize_for_dedupe(
        " ".join(_block_plain_text(block) for block in existing_blocks)
    )
    # Also include raw page text so widget-only duplicates of drawn text are caught
    # even when text landed in skipped table regions.
    page_corpus = normalize_text(
        f"{page_corpus} {_normalize_for_dedupe(page.get_text('text'))}"
    )

    seen: set[str] = set()
    for widget in widgets:
        raw_label = getattr(widget, "field_label", None) or widget.field_name or ""
        label = normalize_text(raw_label)
        if not label or label.lower() in {"undefined", "null", "none"}:
            continue
        key = _normalize_for_dedupe(label)
        if not key or key in seen:
            continue
        seen.add(key)
        # Prefer page text when an equivalent label is already present.
        if key in page_corpus:
            continue
        value = normalize_text(str(widget.field_value or ""))
        text = f"{label}: {value}" if value else label
        blocks.append(ContentBlock(type="paragraph", text=text, page=page_num))
    return blocks


def extract_pdf_document(data: bytes) -> StructuredDocument:
    """Extract structured text from PDF bytes. Does not perform OCR."""
    if not data:
        raise ValueError("empty PDF bytes")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"malformed or unreadable PDF: {exc}") from exc

    try:
        if bool(getattr(doc, "needs_pass", False)):
            raise ValueError("encrypted PDF is not supported")

        meta = doc.metadata or {}
        title = normalize_text(meta.get("title"))
        blocks: list[ContentBlock] = []
        empty_pages: list[int] = []

        for index in range(doc.page_count):
            page = doc.load_page(index)
            page_num = index + 1
            table_rects = _collect_table_rects(page)
            page_blocks = _extract_tables(page, page_num)
            page_blocks.extend(_extract_text_blocks(page, page_num, table_rects))
            page_blocks.extend(_extract_form_labels(page, page_num, page_blocks))
            page_blocks = _merge_consecutive_headings(page_blocks)

            plain = normalize_text(page.get_text("text"))
            if not page_blocks and not plain:
                empty_pages.append(page_num)
            else:
                blocks.extend(page_blocks)

        metadata = {
            key: normalize_text(meta.get(key))
            for key in ("author", "subject", "keywords")
            if normalize_text(meta.get(key))
        }
        return StructuredDocument(
            title=title,
            blocks=blocks,
            page_count=doc.page_count,
            empty_pages=tuple(empty_pages),
            metadata=metadata,
        )
    finally:
        doc.close()


__all__ = [
    "EXTRACTOR_NAME",
    "EXTRACTOR_VERSION",
    "extract_pdf_document",
    "normalized_content_sha256",
]
