"""Lightweight text extraction helpers for external API payloads."""

from __future__ import annotations

import re
from html.parser import HTMLParser


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._in_row = False
        self._row_has_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = (tag or "").lower()
        if t in {"script", "style", "nav", "footer", "header", "aside", "noscript", "template"}:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if t == "tr":
            self._in_row = True
            self._row_has_cell = False
        elif t in {"th", "td"}:
            if self._row_has_cell:
                self._chunks.append(" | ")
            self._row_has_cell = True

    def handle_endtag(self, tag: str) -> None:
        t = (tag or "").lower()
        if t in {"script", "style", "nav", "footer", "header", "aside", "noscript", "template"}:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if t == "tr":
            self._in_row = False
            self._row_has_cell = False
            self._chunks.append("\n")
        elif t in {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "div", "table"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks)


def html_to_text(html: str) -> str:
    stripper = _HTMLStripper()
    try:
        stripper.feed(html)
        stripper.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return normalize_whitespace(stripper.get_text())


def normalize_whitespace(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_passages_with_truncation(
    text: str,
    *,
    max_passages: int = 8,
    max_chars: int = 600,
) -> tuple[list[str], bool]:
    """Split substantive text into whitespace-bounded passages and report truncation."""
    if not text:
        return [], False
    parts = re.split(r"\n{2,}|(?<=[.!?])\s+(?=[A-Z0-9£€$\(])", text)
    out: list[str] = []
    truncated = False
    for idx, part in enumerate(parts):
        cleaned = normalize_whitespace(part)
        if len(cleaned) < 40:
            continue
        if cleaned.count("\n") > 12 and len(cleaned) < 200:
            continue
        if len(cleaned) > max_chars:
            truncated = True
            cut = cleaned[: max_chars - 15].rsplit(" ", 1)[0].rstrip()
            if not cut or len(cut) < max_chars // 2:
                cut = cleaned[: max_chars - 15].rstrip()
            cleaned = f"{cut} …[truncated]"
        out.append(cleaned)
        if len(out) >= max_passages:
            if sum(len(normalize_whitespace(p)) for p in parts[idx + 1 :]) >= 20:
                truncated = True
            break
    if not out and text.strip():
        norm = normalize_whitespace(text)
        if len(norm) > max_chars:
            truncated = True
            cut = norm[: max_chars - 15].rsplit(" ", 1)[0].rstrip()
            snippet = f"{cut or norm[: max_chars - 15].rstrip()} …[truncated]"
        else:
            snippet = norm
        if snippet:
            out.append(snippet)
    return out, truncated


def extract_passages(text: str, *, max_passages: int = 8, max_chars: int = 600) -> list[str]:
    """Split substantive text into short passages; drop tiny/nav crumbs."""
    passages, _ = extract_passages_with_truncation(
        text, max_passages=max_passages, max_chars=max_chars
    )
    return passages


def dig_body_html(details: dict) -> str | None:
    """Pull all substantive HTML body fields from GOV.UK Content API ``details``."""
    if not isinstance(details, dict):
        return None
    collected: list[str] = []
    for key in ("introduction", "body", "need_to_know", "other_ways_to_apply"):
        val = details.get(key)
        if isinstance(val, str) and val.strip():
            collected.append(val)
        elif isinstance(val, list):
            chunks: list[str] = []
            for item in val:
                if isinstance(item, dict) and isinstance(item.get("content"), str):
                    chunks.append(item["content"])
                elif isinstance(item, str):
                    chunks.append(item)
            if chunks:
                collected.append("\n".join(chunks))
    # Nested parts used by multi-part GOV.UK guides
    parts = details.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                body = part.get("body")
                if isinstance(body, str) and body.strip():
                    title = part.get("title") or ""
                    collected.append(f"<h2>{title}</h2>\n{body}" if title else body)
    if collected:
        return "\n\n".join(collected)
    return None
