"""Heading-aware deterministic chunking of structured extractions."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from lifepilot.extract.schema import normalize_text
from lifepilot.rag.clean import block_plain_text, filter_blocks

# Approximate token sizing without a tokenizer dependency.
# Use a conservative chars/token ratio so multilingual text stays under the
# E5 512-token limit (prefix + special tokens). Dense scripts can approach
# ~1.7 chars/token; 800 chars leaves headroom under 512.
_CHARS_PER_TOKEN = 3
DEFAULT_TARGET_TOKENS = 280
DEFAULT_MAX_TOKENS = 380
DEFAULT_MIN_CHARS = 120
DEFAULT_OVERLAP_CHARS = 120
# Hard ceiling after packing — keeps passage embeddings within E5 max length.
# Dense multilingual scripts can approach ~1.5 chars/token; 720 leaves headroom
# under 512 including the passage: prefix and special tokens.
E5_SAFE_MAX_CHARS = 720


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
    table_header: str | None = None


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
        table_header: str | None = None
        if typ == "table":
            hdr_parts: list[str] = []
            cap = normalize_text(str(block.get("caption") or ""))
            if cap:
                hdr_parts.append(cap)
            hdrs = block.get("headers") or []
            if hdrs:
                hdr_text = " | ".join(normalize_text(str(h)) for h in hdrs if normalize_text(str(h)))
                if hdr_text:
                    hdr_parts.append(hdr_text)
            if hdr_parts:
                table_header = "\n".join(hdr_parts)
        units.append(
            _Unit(
                text=text,
                heading_path=path,
                is_heading=False,
                table_header=table_header,
            )
        )
    return units


def _safe_slice_end(text: str, max_chars: int) -> int:
    """Prefer whitespace cut before max_chars so words/numbers are not sheared."""
    if len(text) <= max_chars:
        return len(text)
    cut = text[:max_chars]
    ws = max(cut.rfind("\n"), cut.rfind(" "))
    if ws >= max_chars // 2:
        return ws
    return max_chars


def _safe_tail(buf: str, overlap_chars: int) -> str:
    if not overlap_chars or len(buf) <= overlap_chars:
        return ""
    raw = buf[-overlap_chars:]
    first_space = raw.find(" ")
    if 0 < first_space < len(raw) - 10:
        return raw[first_space + 1 :].lstrip()
    return raw.lstrip()


def _split_long_text(
    text: str,
    *,
    max_chars: int,
    overlap_chars: int,
    header_prefix: str | None = None,
) -> list[str]:
    """Split oversized unit text on paragraph/sentence/whitespace boundaries."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    prefix = (header_prefix or "").strip()
    use_prefix = bool(prefix and len(prefix) + 40 < max_chars)
    parts = re.split(r"\n+", text)
    if len(parts) == 1:
        parts = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    buf = ""

    def _hard_wrap(chunk_str: str) -> str:
        while len(chunk_str) > max_chars:
            cut_idx = _safe_slice_end(chunk_str, max_chars)
            out.append(chunk_str[:cut_idx].rstrip())
            step_start = max(1, cut_idx - min(overlap_chars, cut_idx // 2))
            if step_start < cut_idx:
                sp = chunk_str.find(" ", step_start, cut_idx)
                if sp != -1:
                    step_start = sp + 1
            remainder = chunk_str[step_start:].lstrip()
            if use_prefix and not remainder.startswith(prefix):
                remainder = f"{prefix}\n{remainder}".strip()
            chunk_str = remainder
        return chunk_str

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
            if use_prefix and not part.startswith(prefix):
                buf = f"{prefix}\n{part}".strip()
            else:
                tail = _safe_tail(buf, overlap_chars)
                buf = f"{tail}\n{part}".strip() if tail else part
            if len(buf) > max_chars:
                buf = _hard_wrap(buf)
        else:
            buf = _hard_wrap(part)
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
            if (
                len(merged) <= E5_SAFE_MAX_CHARS
                and _estimate_tokens(merged) <= max_tokens + 50
            ):
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
            unit.text,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            header_prefix=unit.table_header,
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
            # If buffer filled to target tokens, allow flush at max_tokens.
            if _estimate_tokens("\n\n".join(buf_parts)) >= target_tokens and not unit.is_heading:
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
                # Do not re-create oversize chunks when merging tiny remainders.
                if len(merged) > E5_SAFE_MAX_CHARS:
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
                else:
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

    # Final hard split so no draft exceeds the E5-safe character ceiling.
    return _enforce_max_chars(
        drafts, max_chars=E5_SAFE_MAX_CHARS, overlap_chars=overlap_chars
    )


def _detect_table_header_prefix(text: str) -> str | None:
    """Detect a table column header line near the top of a chunk for continuation splits."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for idx, ln in enumerate(lines[:5]):
        if " | " in ln and len(ln) <= 180:
            # Prefer the first ' | ' row (which in block_plain_text is always the headers row).
            if idx > 0 and " | " not in lines[idx - 1] and len(lines[idx - 1]) <= 120:
                # Include immediate caption if short and non-sentence, else just header line.
                cap = lines[idx - 1]
                if not cap.endswith((".", "!", "?")) and idx == 1:
                    return f"{cap}\n{ln}"
            return ln
    return None


def _enforce_max_chars(
    drafts: list[ChunkDraft],
    *,
    max_chars: int,
    overlap_chars: int,
) -> list[ChunkDraft]:
    """Split any draft whose text exceeds ``max_chars`` on soft boundaries."""
    out: list[ChunkDraft] = []
    for draft in drafts:
        if len(draft.content_text) <= max_chars:
            out.append(draft)
            continue
        hdr = _detect_table_header_prefix(draft.content_text)
        pieces = _split_long_text(
            draft.content_text,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            header_prefix=hdr,
        )
        offset = draft.char_start or 0
        for piece in pieces:
            out.append(
                ChunkDraft(
                    chunk_index=len(out),
                    content_text=piece,
                    content_sha256=_sha256_text(piece),
                    heading_path=draft.heading_path,
                    char_start=offset,
                    char_end=offset + len(piece),
                )
            )
            offset += len(piece)
    return [
        ChunkDraft(
            chunk_index=i,
            content_text=d.content_text,
            content_sha256=d.content_sha256,
            heading_path=d.heading_path,
            char_start=d.char_start,
            char_end=d.char_end,
        )
        for i, d in enumerate(out)
    ]


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
