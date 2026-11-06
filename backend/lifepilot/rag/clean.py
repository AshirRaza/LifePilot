"""Boilerplate / navigation filtering for structured extraction blocks."""

from __future__ import annotations

import re
from typing import Any

from lifepilot.extract.schema import normalize_text

# Blocks matching these patterns (as whole-block text) are dropped.
_BOILERPLATE_EXACT = re.compile(
    r"^(?:"
    r"skip to (?:main )?content|skip to navigation|"
    r"cookie(?:s)? (?:policy|settings|preferences|notice|banner)|"
    r"we use cookies|accept (?:all )?cookies|manage cookies|"
    r"javascript (?:is )?(?:required|disabled)|"
    r"enable javascript|"
    r"print (?:this )?(?:page|article)|"
    r"share (?:this )?(?:page|article)|"
    r"subscribe to (?:emails?|updates?|alerts?)|"
    r"sign in|log in|create (?:an )?account|"
    r"back to top|return to top|"
    r"accessibility(?: help| statement)?|"
    r"this website uses cookies"
    r")\.?$",
    re.I,
)

_BOILERPLATE_CONTAINS = re.compile(
    r"(?:"
    r"we use cookies|"
    r"cookies? (?:help us|on this site|we use)|"
    r"necessary cookies|"
    r"strictly necessary|"
    r"your cookie preferences|"
    r"skip to main content|"
    r"browser does not support"
    r")",
    re.I,
)

# Very short chrome that is never substantive immigration guidance.
_MIN_SUBSTANTIVE_CHARS = 25


def block_plain_text(block: dict[str, Any]) -> str:
    """Flatten a structured block to plain text for filtering/chunking."""
    typ = str(block.get("type") or "").lower()
    if typ == "heading":
        return normalize_text(str(block.get("text") or ""))
    if typ == "paragraph":
        return normalize_text(str(block.get("text") or ""))
    if typ == "list":
        items = block.get("items") or []
        parts = [normalize_text(str(i)) for i in items if normalize_text(str(i))]
        return "\n".join(f"• {p}" for p in parts)
    if typ == "table":
        chunks: list[str] = []
        caption = normalize_text(str(block.get("caption") or ""))
        if caption:
            chunks.append(caption)
        headers = block.get("headers") or []
        if headers:
            chunks.append(" | ".join(normalize_text(str(h)) for h in headers))
        for row in block.get("rows") or []:
            if isinstance(row, (list, tuple)):
                chunks.append(" | ".join(normalize_text(str(c)) for c in row))
        return "\n".join(c for c in chunks if c)
    return normalize_text(str(block.get("text") or ""))


def is_boilerplate_block(block: dict[str, Any]) -> bool:
    """Return True when a block is navigation/cookie chrome or empty."""
    typ = str(block.get("type") or "").lower()
    text = block_plain_text(block)
    if not text:
        return True
    if typ == "heading":
        # Keep real section headings even if short; drop chrome headings.
        if _BOILERPLATE_EXACT.match(text) or _BOILERPLATE_CONTAINS.search(text):
            return True
        return False
    if _BOILERPLATE_EXACT.match(text):
        return True
    if len(text) < 80 and _BOILERPLATE_CONTAINS.search(text):
        return True
    if typ == "paragraph" and len(text) < _MIN_SUBSTANTIVE_CHARS:
        return True
    return False


def filter_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop boilerplate while preserving order of substantive blocks."""
    return [b for b in blocks if isinstance(b, dict) and not is_boilerplate_block(b)]
