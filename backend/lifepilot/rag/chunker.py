"""Heading-aware deterministic chunking of structured extractions."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from lifepilot.extract.schema import normalize_text
from lifepilot.rag.clean import block_plain_text, filter_blocks

# Approximate English token sizing without a tokenizer dependency.
_CHARS_PER_TOKEN = 4
DEFAULT_TARGET_TOKENS = 400
DEFAULT_MAX_TOKENS = 550
DEFAULT_MIN_CHARS = 120
DEFAULT_OVERLAP_CHARS = 160


@dataclass(frozen=True)
class ChunkDraft:
    chunk_index: int
    content_text: str
    content_sha256: str
    heading_path: str | None
    char_start: int | None
    char_end: int | None


@dataclass
class _Unit:
    text: str
    heading_path: str
    is_heading: bool = False


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _heading_level(block: dict[str, Any]) -> int:
    level = block.get("level")
    try:
        n = int(level) if level is not None else 2
    except (TypeError, ValueError):
        n = 2
    return max(1, min(n, 6))


def _update_heading_stack(
    stack: list[tuple[int, str]], level: int, title: str
) -> list[tuple[int, str]]:
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, title))
    return stack


def _path_from_stack(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack)


def blocks_to_units(blocks: list[dict[str, Any]], *, title: str | None) -> list[_Unit]:
    """Convert filtered blocks into linear units with heading paths."""
    stack: list[tuple[int, str]] = []
    if title and normalize_text(title):
        stack = [(1, normalize_text(title))]
    units: list[_Unit] = []
    for block in blocks:
        typ = str(block.get("type") or "").lower()
        text = block_plain_text(block)
        if not text:
            continue
        if typ == "heading":
            level = _heading_level(block)
            _update_heading_stack(stack, level, text)
            path = _path_from_stack(stack)
            units.append(_Unit(text=text, heading_path=path, is_heading=True))
            continue
        path = _path_from_stack(stack) or (normalize_text(title) if title else "")
        units.append(_Unit(text=text, heading_path=path, is_heading=False))
    return units


def _split_long_text(text: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    """Split oversized unit text on paragraph/sentence boundaries."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    # Prefer blank-line / newline splits, then sentences.
    parts = re.split(r"\n+", text)
    if len(parts) == 1:
        parts = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    buf = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        candidate = f"{buf}\n{part}".strip() if buf else part
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf:
            out.append(buf)
            # Limited overlap from end of previous buffer.
            tail = buf[-overlap_chars:] if overlap_chars and len(buf) > overlap_chars else ""
            buf = f"{tail}\n{part}".strip() if tail else part
            if len(buf) > max_chars:
                # Hard wrap remaining long atom.
                while len(buf) > max_chars:
                    out.append(buf[:max_chars].rstrip())
                    start = max(0, max_chars - overlap_chars)
                    buf = buf[start:].lstrip()
        else:
            while len(part) > max_chars:
                out.append(part[:max_chars].rstrip())
                start = max(0, max_chars - overlap_chars)
                part = part[start:].lstrip()
            buf = part
    if buf:
        out.append(buf)
    return out


def chunk_units(
    units: list[_Unit],
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    min_chars: int = DEFAULT_MIN_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[ChunkDraft]:
    """Pack units into ~300–500 token chunks, preferring heading boundaries."""
    target_chars = target_tokens * _CHARS_PER_TOKEN
    max_chars = max_tokens * _CHARS_PER_TOKEN
    drafts: list[ChunkDraft] = []

    buf_parts: list[str] = []
    buf_path = ""
    buf_start = 0
    cursor = 0

    def flush() -> None:
        nonlocal buf_parts, buf_path, buf_start
        if not buf_parts:
            return
        content = "\n\n".join(buf_parts).strip()
        if len(content) < min_chars and drafts:
            # Merge tiny remainder into previous chunk when possible.
            prev = drafts[-1]
            merged = f"{prev.content_text}\n\n{content}".strip()
            if _estimate_tokens(merged) <= max_tokens + 50:
                drafts[-1] = ChunkDraft(
                    chunk_index=prev.chunk_index,
                    content_text=merged,
                    content_sha256=_sha256_text(merged),
                    heading_path=prev.heading_path,
                    char_start=prev.char_start,
                    char_end=(prev.char_start or 0) + len(merged),
                )
                buf_parts = []
                buf_path = ""
                return
        if not content:
            buf_parts = []
            buf_path = ""
            return
        end = buf_start + len(content)
        drafts.append(
            ChunkDraft(
                chunk_index=len(drafts),
                content_text=content,
                content_sha256=_sha256_text(content),
                heading_path=buf_path or None,
                char_start=buf_start,
                char_end=end,
            )
        )
        buf_parts = []
        buf_path = ""

    for unit in units:
        pieces = _split_long_text(
            unit.text, max_chars=max_chars, overlap_chars=overlap_chars
        )
        for piece in pieces:
            # Start a new chunk at a heading when the buffer is already healthy.
            if (
                unit.is_heading
                and buf_parts
                and _estimate_tokens("\n\n".join(buf_parts)) >= target_tokens // 2
            ):
                flush()
                buf_start = cursor

            projected = (
                "\n\n".join([*buf_parts, piece]).strip() if buf_parts else piece
            )
            if buf_parts and _estimate_tokens(projected) > max_tokens:
                flush()
                buf_start = cursor

            if not buf_parts:
                buf_start = cursor
                buf_path = unit.heading_path
            buf_parts.append(piece)
            # If a single piece filled near max, flush.
            if _estimate_tokens("\n\n".join(buf_parts)) >= target_chars and not unit.is_heading:
                # Keep going until next heading-ish break unless over max.
                if _estimate_tokens("\n\n".join(buf_parts)) >= max_tokens:
                    flush()
                    buf_start = cursor + len(piece) + 2
            cursor += len(piece) + 2

    flush()

    # Drop still-too-short orphans unless they are the only chunk.
    if len(drafts) > 1:
        kept: list[ChunkDraft] = []
        for i, d in enumerate(drafts):
            if len(d.content_text) < min_chars and kept:
                merged = f"{kept[-1].content_text}\n\n{d.content_text}".strip()
                prev = kept[-1]
                kept[-1] = ChunkDraft(
                    chunk_index=prev.chunk_index,
                    content_text=merged,
                    content_sha256=_sha256_text(merged),
                    heading_path=prev.heading_path,
                    char_start=prev.char_start,
                    char_end=(prev.char_start or 0) + len(merged),
                )
            else:
                kept.append(
                    ChunkDraft(
                        chunk_index=len(kept),
                        content_text=d.content_text,
                        content_sha256=d.content_sha256,
                        heading_path=d.heading_path,
                        char_start=d.char_start,
                        char_end=d.char_end,
                    )
                )
        drafts = kept
    else:
        drafts = [
            ChunkDraft(
                chunk_index=i,
                content_text=d.content_text,
                content_sha256=d.content_sha256,
                heading_path=d.heading_path,
                char_start=d.char_start,
                char_end=d.char_end,
            )
            for i, d in enumerate(drafts)
        ]

    return drafts


def chunk_structured_document(
    payload: dict[str, Any],
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    min_chars: int = DEFAULT_MIN_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[ChunkDraft]:
    """Chunk one structured extraction JSON object into drafts."""
    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list):
        return []
    blocks = filter_blocks([b for b in raw_blocks if isinstance(b, dict)])
    title = payload.get("title") if isinstance(payload.get("title"), str) else None
    units = blocks_to_units(blocks, title=title)
    if not units:
        return []
    return chunk_units(
        units,
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        min_chars=min_chars,
        overlap_chars=overlap_chars,
    )
