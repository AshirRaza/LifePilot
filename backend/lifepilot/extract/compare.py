"""Meaningful content comparison between structured extractions.

Compares extracted StructuredDocument content across document versions.
Raw byte hashes alone never imply a meaningful change. Deterministic;
no LLM classification.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from lifepilot.extract.schema import (
    SCHEMA_VERSION,
    ContentBlock,
    StructuredDocument,
    normalize_text,
    normalized_content_sha256,
)

COMPARISON_ALGORITHM_VERSION = "1.0.0"

STATUS_UNCHANGED = "unchanged"
STATUS_CHANGED = "changed"
STATUS_UNAVAILABLE = "unavailable"


def _block_text_signature(block: ContentBlock) -> str:
    """Stable signature for sequence matching (excludes page layout)."""
    if block.type == "heading":
        return f"heading|{block.level}|{block.text or ''}"
    if block.type == "paragraph":
        return f"paragraph|{block.text or ''}"
    if block.type == "list":
        items = "\n".join(block.items or ())
        return f"list|{int(bool(block.ordered))}|{items}"
    if block.type == "table":
        headers = "\t".join(block.headers or ())
        rows = "\n".join("\t".join(row) for row in (block.rows or ()))
        return f"table|{block.caption or ''}|{headers}|{rows}"
    return f"{block.type}|{block.text or ''}"


def _block_payload(block: ContentBlock, *, include_page: bool = True) -> dict[str, Any]:
    data = block.to_dict()
    if not include_page:
        data.pop("page", None)
    return data


def text_content_dict(document: StructuredDocument) -> dict[str, Any]:
    """Content dict used for textual equality (excludes page layout fields)."""
    return {
        "title": document.title,
        "blocks": [
            _block_payload(block, include_page=False) for block in document.blocks
        ],
    }


def text_content_sha256(document: StructuredDocument) -> str:
    canonical = json.dumps(
        text_content_dict(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _nearest_heading(blocks: list[ContentBlock], index: int) -> str | None:
    for i in range(index, -1, -1):
        block = blocks[i]
        if block.type == "heading" and block.text:
            return block.text
    return None


def _list_item_changes(
    previous: ContentBlock, current: ContentBlock
) -> list[dict[str, Any]]:
    prev_items = list(previous.items or ())
    curr_items = list(current.items or ())
    matcher = SequenceMatcher(a=prev_items, b=curr_items, autojunk=False)
    details: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            for item in prev_items[i1:i2]:
                details.append({"op": "removed_item", "text": item})
        elif tag == "insert":
            for item in curr_items[j1:j2]:
                details.append({"op": "added_item", "text": item})
        else:  # replace
            for item in prev_items[i1:i2]:
                details.append({"op": "removed_item", "text": item})
            for item in curr_items[j1:j2]:
                details.append({"op": "added_item", "text": item})
    return details


def _table_cell_changes(
    previous: ContentBlock, current: ContentBlock
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    if (previous.caption or "") != (current.caption or ""):
        details.append(
            {
                "op": "modified_caption",
                "previous": previous.caption,
                "current": current.caption,
            }
        )
    prev_headers = list(previous.headers or ())
    curr_headers = list(current.headers or ())
    max_h = max(len(prev_headers), len(curr_headers))
    for col in range(max_h):
        p = prev_headers[col] if col < len(prev_headers) else None
        c = curr_headers[col] if col < len(curr_headers) else None
        if p != c:
            details.append(
                {
                    "op": "modified_header",
                    "column": col,
                    "previous": p,
                    "current": c,
                }
            )
    prev_rows = [list(row) for row in (previous.rows or ())]
    curr_rows = [list(row) for row in (current.rows or ())]
    max_r = max(len(prev_rows), len(curr_rows))
    for r in range(max_r):
        prow = prev_rows[r] if r < len(prev_rows) else []
        crow = curr_rows[r] if r < len(curr_rows) else []
        max_c = max(len(prow), len(crow))
        for c in range(max_c):
            pv = prow[c] if c < len(prow) else None
            cv = crow[c] if c < len(crow) else None
            if pv != cv:
                details.append(
                    {
                        "op": "modified_cell",
                        "row": r,
                        "column": c,
                        "previous": pv,
                        "current": cv,
                    }
                )
    return details


def _change_entry(
    *,
    op: str,
    block_type: str,
    previous_blocks: list[ContentBlock],
    current_blocks: list[ContentBlock],
    previous_index: int | None,
    current_index: int | None,
    previous: ContentBlock | None,
    current: ContentBlock | None,
    detail: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    section = None
    if current_index is not None:
        section = _nearest_heading(current_blocks, current_index)
    elif previous_index is not None:
        section = _nearest_heading(previous_blocks, previous_index)
    entry: dict[str, Any] = {
        "op": op,
        "block_type": block_type,
        "section_heading": section,
        "previous_index": previous_index,
        "current_index": current_index,
        "previous_page": previous.page if previous is not None else None,
        "current_page": current.page if current is not None else None,
        "previous": _block_payload(previous) if previous is not None else None,
        "current": _block_payload(current) if current is not None else None,
    }
    if detail:
        entry["detail"] = detail
    return entry


def diff_structured_documents(
    previous: StructuredDocument,
    current: StructuredDocument,
) -> dict[str, Any]:
    """Build a deterministic structured diff between two extractions."""
    prev_blocks = list(previous.blocks)
    curr_blocks = list(current.blocks)
    prev_sigs = [_block_text_signature(b) for b in prev_blocks]
    curr_sigs = [_block_text_signature(b) for b in curr_blocks]
    matcher = SequenceMatcher(a=prev_sigs, b=curr_sigs, autojunk=False)

    changes: list[dict[str, Any]] = []
    layout_differences: list[dict[str, Any]] = []

    title_prev = normalize_text(previous.title)
    title_curr = normalize_text(current.title)
    title_changed = title_prev != title_curr
    if title_changed:
        changes.append(
            {
                "op": "modified",
                "block_type": "title",
                "section_heading": None,
                "previous_index": None,
                "current_index": None,
                "previous_page": None,
                "current_page": None,
                "previous": {"text": title_prev},
                "current": {"text": title_curr},
            }
        )

    if previous.page_count != current.page_count:
        layout_differences.append(
            {
                "field": "page_count",
                "previous": previous.page_count,
                "current": current.page_count,
            }
        )
    if tuple(previous.empty_pages) != tuple(current.empty_pages):
        layout_differences.append(
            {
                "field": "empty_pages",
                "previous": list(previous.empty_pages),
                "current": list(current.empty_pages),
            }
        )

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset, (pb, cb) in enumerate(
                zip(prev_blocks[i1:i2], curr_blocks[j1:j2])
            ):
                if pb.page != cb.page:
                    layout_differences.append(
                        {
                            "field": "block_page",
                            "previous_index": i1 + offset,
                            "current_index": j1 + offset,
                            "block_type": pb.type,
                            "section_heading": _nearest_heading(
                                curr_blocks, j1 + offset
                            ),
                            "previous_page": pb.page,
                            "current_page": cb.page,
                        }
                    )
            continue

        if tag == "delete":
            for idx in range(i1, i2):
                block = prev_blocks[idx]
                changes.append(
                    _change_entry(
                        op="removed",
                        block_type=block.type,
                        previous_blocks=prev_blocks,
                        current_blocks=curr_blocks,
                        previous_index=idx,
                        current_index=None,
                        previous=block,
                        current=None,
                    )
                )
            continue

        if tag == "insert":
            for idx in range(j1, j2):
                block = curr_blocks[idx]
                changes.append(
                    _change_entry(
                        op="added",
                        block_type=block.type,
                        previous_blocks=prev_blocks,
                        current_blocks=curr_blocks,
                        previous_index=None,
                        current_index=idx,
                        previous=None,
                        current=block,
                    )
                )
            continue

        # replace — conservative: 1:1 same type → modified; else remove+add
        prev_slice = prev_blocks[i1:i2]
        curr_slice = curr_blocks[j1:j2]
        if len(prev_slice) == 1 and len(curr_slice) == 1 and prev_slice[0].type == curr_slice[0].type:
            pb, cb = prev_slice[0], curr_slice[0]
            detail: list[dict[str, Any]] | None = None
            if pb.type == "list":
                detail = _list_item_changes(pb, cb) or None
            elif pb.type == "table":
                detail = _table_cell_changes(pb, cb) or None
            changes.append(
                _change_entry(
                    op="modified",
                    block_type=pb.type,
                    previous_blocks=prev_blocks,
                    current_blocks=curr_blocks,
                    previous_index=i1,
                    current_index=j1,
                    previous=pb,
                    current=cb,
                    detail=detail,
                )
            )
        else:
            for idx, block in enumerate(prev_slice):
                changes.append(
                    _change_entry(
                        op="removed",
                        block_type=block.type,
                        previous_blocks=prev_blocks,
                        current_blocks=curr_blocks,
                        previous_index=i1 + idx,
                        current_index=None,
                        previous=block,
                        current=None,
                    )
                )
            for idx, block in enumerate(curr_slice):
                changes.append(
                    _change_entry(
                        op="added",
                        block_type=block.type,
                        previous_blocks=prev_blocks,
                        current_blocks=curr_blocks,
                        previous_index=None,
                        current_index=j1 + idx,
                        previous=None,
                        current=block,
                    )
                )

    summary = {
        "added": sum(1 for c in changes if c["op"] == "added"),
        "removed": sum(1 for c in changes if c["op"] == "removed"),
        "modified": sum(1 for c in changes if c["op"] == "modified"),
        "layout_differences": len(layout_differences),
    }
    text_equal = text_content_sha256(previous) == text_content_sha256(current)
    full_equal = normalized_content_sha256(previous) == normalized_content_sha256(
        current
    )
    layout_only = text_equal and not full_equal

    if text_equal:
        status = STATUS_UNCHANGED
    else:
        status = STATUS_CHANGED

    return {
        "algorithm_version": COMPARISON_ALGORITHM_VERSION,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "title_changed": title_changed,
        "previous_title": previous.title,
        "current_title": current.title,
        "layout_only": layout_only,
        "layout_differences": layout_differences,
        "changes": changes if not text_equal else [],
        "summary": summary if not text_equal else {
            "added": 0,
            "removed": 0,
            "modified": 0,
            "layout_differences": len(layout_differences),
        },
        "previous_text_sha256": text_content_sha256(previous),
        "current_text_sha256": text_content_sha256(current),
        "previous_content_sha256": normalized_content_sha256(previous),
        "current_content_sha256": normalized_content_sha256(current),
    }


@dataclass
class ComparisonResult:
    document_id: int
    previous_version_id: int | None
    current_version_id: int | None
    status: str
    extractor_name: str | None = None
    extractor_version: str | None = None
    algorithm_version: str = COMPARISON_ALGORITHM_VERSION
    previous_content_sha256: str | None = None
    current_content_sha256: str | None = None
    previous_version_number: int | None = None
    current_version_number: int | None = None
    raw_sha256_equal: bool | None = None
    layout_only: bool = False
    unavailable_reason: str | None = None
    diff: dict[str, Any] | None = None
    storage_path: str | None = None
    comparison_id: int | None = None
    message: str = ""

    def summary_line(self) -> str:
        if self.status == STATUS_UNCHANGED:
            if self.layout_only:
                return "unchanged textual content (layout-only differences)"
            return "unchanged extracted content"
        if self.status == STATUS_CHANGED:
            summary = (self.diff or {}).get("summary") or {}
            return (
                f"changed: +{summary.get('added', 0)} "
                f"-{summary.get('removed', 0)} "
                f"~{summary.get('modified', 0)}"
            )
        return self.unavailable_reason or self.message or "unavailable"


@dataclass
class ComparisonBatchReport:
    results: list[ComparisonResult] = field(default_factory=list)
    message: str = ""

    @property
    def unchanged_count(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_UNCHANGED)

    @property
    def changed_count(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_CHANGED)

    @property
    def unavailable_count(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_UNAVAILABLE)
