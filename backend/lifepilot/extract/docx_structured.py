"""Structured text extraction from stored DOCX document versions."""

from __future__ import annotations

from io import BytesIO

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from lifepilot.extract.schema import (
    ContentBlock,
    StructuredDocument,
    normalize_text,
    normalized_content_sha256,
)

EXTRACTOR_NAME = "docx_structured"
EXTRACTOR_VERSION = "1.0.0"


def _heading_level(style_name: str) -> int | None:
    name = style_name or ""
    lower = name.lower()
    if lower.startswith("heading"):
        suffix = lower.replace("heading", "", 1).strip()
        if suffix.isdigit():
            level = int(suffix)
            if 1 <= level <= 6:
                return level
        return 1
    if lower in {"title"}:
        return 1
    if lower in {"subtitle"}:
        return 2
    return None


def _list_info(paragraph: Paragraph) -> tuple[bool, bool]:
    """Return (is_list_item, ordered)."""
    style_name = (paragraph.style.name if paragraph.style is not None else "") or ""
    lower = style_name.lower()
    if "list bullet" in lower or lower.endswith("bullet"):
        return True, False
    if "list number" in lower or "list numbering" in lower or "number" in lower and "list" in lower:
        return True, True
    # numPr on the paragraph indicates a Word list.
    p_pr = paragraph._p.pPr  # noqa: SLF001 — python-docx public enough for list detection
    if p_pr is not None and p_pr.numPr is not None:
        # Treat as unordered unless style suggests numbering.
        ordered = "number" in lower
        return True, ordered
    return False, False


def _paragraph_block(paragraph: Paragraph) -> ContentBlock | None:
    text = normalize_text(paragraph.text)
    if not text:
        return None
    style_name = (paragraph.style.name if paragraph.style is not None else "") or ""
    level = _heading_level(style_name)
    if level is not None:
        return ContentBlock(type="heading", level=level, text=text)
    is_list, ordered = _list_info(paragraph)
    if is_list:
        return ContentBlock(type="list", ordered=ordered, items=(text,))
    return ContentBlock(type="paragraph", text=text)


def _table_block(table: Table) -> ContentBlock | None:
    rows: list[tuple[str, ...]] = []
    for row in table.rows:
        cells = tuple(normalize_text(cell.text) for cell in row.cells)
        if any(cells):
            rows.append(cells)
    if not rows:
        return None
    headers: tuple[str, ...] = ()
    body = rows
    if len(rows) >= 2 and any(rows[0]):
        headers = rows[0]
        body = rows[1:]
    return ContentBlock(type="table", headers=headers, rows=tuple(body))


def _merge_adjacent_list_items(blocks: list[ContentBlock]) -> list[ContentBlock]:
    """Collapse consecutive single-item list blocks with the same ordered flag."""
    merged: list[ContentBlock] = []
    for block in blocks:
        if (
            block.type == "list"
            and merged
            and merged[-1].type == "list"
            and merged[-1].ordered == block.ordered
            and block.page is None
            and merged[-1].page is None
        ):
            prev = merged[-1]
            merged[-1] = ContentBlock(
                type="list",
                ordered=prev.ordered,
                items=tuple(prev.items or ()) + tuple(block.items or ()),
            )
        else:
            merged.append(block)
    return merged


def extract_docx_document(data: bytes) -> StructuredDocument:
    """Extract structured content from DOCX bytes. No synthetic page numbers."""
    if not data:
        raise ValueError("empty DOCX bytes")
    try:
        document = Document(BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"malformed or unreadable DOCX: {exc}") from exc

    blocks: list[ContentBlock] = []
    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag
        if tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            block = _paragraph_block(paragraph)
            if block is not None:
                blocks.append(block)
        elif tag == qn("w:tbl"):
            table = Table(child, document)
            block = _table_block(table)
            if block is not None:
                blocks.append(block)

    blocks = _merge_adjacent_list_items(blocks)

    props = document.core_properties
    title = normalize_text(props.title) or ""
    if not title:
        for block in blocks:
            if block.type == "heading" and block.text:
                title = block.text
                break

    metadata: dict[str, str] = {}
    if normalize_text(props.author):
        metadata["author"] = normalize_text(props.author)
    if normalize_text(props.subject):
        metadata["subject"] = normalize_text(props.subject)
    if normalize_text(props.keywords):
        metadata["keywords"] = normalize_text(props.keywords)
    if props.created is not None:
        metadata["created"] = props.created.isoformat()
    if props.modified is not None:
        metadata["modified"] = props.modified.isoformat()

    return StructuredDocument(
        title=title,
        blocks=blocks,
        metadata=metadata,
    )


__all__ = [
    "EXTRACTOR_NAME",
    "EXTRACTOR_VERSION",
    "extract_docx_document",
    "normalized_content_sha256",
]
