"""Lightweight content-language hints for stored documents.

Used so PDF language metadata is not inferred from URL path segments such as
``/en/forms/`` when the body is Dutch (common for IND publications).
"""

from __future__ import annotations

import re

_EN_HINTS = (
    " the ",
    " and ",
    " you ",
    " your ",
    " with ",
    " application ",
    " residence ",
    " must ",
)
_NL_HINTS = (
    " de ",
    " het ",
    " een ",
    " van ",
    " voor ",
    " nederlander ",
    " nationaliteit ",
    " naturalisatie ",
    " u ",
    " uw ",
)


def infer_text_language(sample: str) -> str | None:
    """Return ``en`` / ``nl`` when stopword evidence is clear; else None."""
    text = f" {re.sub(r'\s+', ' ', (sample or '').lower())} "
    if len(text.strip()) < 40:
        return None
    en = sum(1 for w in _EN_HINTS if w in text)
    nl = sum(1 for w in _NL_HINTS if w in text)
    if nl >= en + 2 and nl >= 3:
        return "nl"
    if en >= nl + 2 and en >= 3:
        return "en"
    return None


def infer_pdf_language(body: bytes, *, max_pages: int = 2) -> str | None:
    """Sample the first PDF pages and infer language from text content."""
    if not body or not body.lstrip().startswith(b"%PDF"):
        return None
    try:
        import pymupdf
    except ImportError:
        return None
    try:
        doc = pymupdf.open(stream=body, filetype="pdf")
    except Exception:  # noqa: BLE001
        return None
    try:
        chunks: list[str] = []
        for index in range(min(int(doc.page_count or 0), max_pages)):
            page = doc.load_page(index)
            chunks.append(page.get_text("text") or "")
        return infer_text_language("\n".join(chunks))
    finally:
        doc.close()


__all__ = ["infer_pdf_language", "infer_text_language"]
