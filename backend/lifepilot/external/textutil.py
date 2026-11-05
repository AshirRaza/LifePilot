"""Lightweight text extraction helpers for external API payloads."""

from __future__ import annotations

import re
from html.parser import HTMLParser


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "nav", "footer", "header"}:
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "footer", "header"}:
            self._skip = False
        if tag in {"p", "li", "h1", "h2", "h3", "h4", "br", "div", "tr"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and data:
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


def extract_passages(text: str, *, max_passages: int = 8, max_chars: int = 600) -> list[str]:
    """Split substantive text into short passages; drop tiny/nav crumbs."""
    if not text:
        return []
    parts = re.split(r"\n{2,}|(?<=[.!?])\s+(?=[A-Z])", text)
    out: list[str] = []
    for part in parts:
        cleaned = normalize_whitespace(part)
        if len(cleaned) < 40:
            continue
        # Skip menu-like crumbs
        if cleaned.count("\n") > 12 and len(cleaned) < 200:
            continue
        if len(cleaned) > max_chars:
            cleaned = cleaned[: max_chars - 1].rstrip() + "…"
        out.append(cleaned)
        if len(out) >= max_passages:
            break
    if not out and text.strip():
        snippet = normalize_whitespace(text)[:max_chars]
        if snippet:
            out.append(snippet)
    return out


def dig_body_html(details: dict) -> str | None:
    """Pull HTML body fields from GOV.UK Content API ``details``."""
    if not isinstance(details, dict):
        return None
    for key in ("body", "introduction", "need_to_know", "other_ways_to_apply"):
        val = details.get(key)
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, list):
            chunks: list[str] = []
            for item in val:
                if isinstance(item, dict) and isinstance(item.get("content"), str):
                    chunks.append(item["content"])
                elif isinstance(item, str):
                    chunks.append(item)
            if chunks:
                return "\n".join(chunks)
    # Nested parts used by some schemas
    parts = details.get("parts")
    if isinstance(parts, list):
        chunks = []
        for part in parts:
            if isinstance(part, dict):
                body = part.get("body")
                if isinstance(body, str):
                    title = part.get("title") or ""
                    chunks.append(f"{title}\n{body}" if title else body)
        if chunks:
            return "\n\n".join(chunks)
    return None
