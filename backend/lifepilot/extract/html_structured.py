"""Deterministic structured extraction from stored HTML pages.

Produces ordered blocks (headings, paragraphs, lists, tables) suitable for
later citation-grounded chunking. Does not fetch the network or mutate raw
files.
"""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from lifepilot.extract.schema import (
    SCHEMA_VERSION,
    ContentBlock,
    StructuredDocument,
    normalize_text,
    normalized_content_sha256,
)

EXTRACTOR_NAME = "html_structured"
EXTRACTOR_VERSION = "1.1.0"

# Prefer these roots when present (government sites).
_MAIN_SELECTORS = (
    "main#content",
    "main",
    "article",
    "[role='main']",
    "#content",
    "#main-content",
    ".main-content",
    "#main",
)

_STRIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "iframe",
        "canvas",
        "object",
        "embed",
        "form",
        "button",
        "input",
        "select",
        "textarea",
        "nav",
        "header",
        "footer",
        "aside",
    }
)

_BOILERPLATE_ROLES = frozenset(
    {"navigation", "banner", "contentinfo", "complementary", "search"}
)

# Substring match against id/class (lowercased). Keep conservative.
_BOILERPLATE_TOKENS = (
    "cookie",
    "cookie-banner",
    "cookie_banner",
    "cookies-banner",
    "govuk-cookie",
    "gem-c-cookie",
    "global-cookie",
    "consent",
    "newsletter",
    "subscribe",
    "social-share",
    "share-links",
    "related-navigation",
    "related-nav",
    "breadcrumb",
    "breadcrumbs",
    "skip-link",
    "skiplink",
    "phase-banner",
    "govuk-phase-banner",
    "feedback",
    "gem-c-feedback",
    "print-link",
    "back-to-top",
    "sticky-element",
    "chat-widget",
    "live-chat",
)


def _attr_blob(tag: Tag) -> str:
    parts: list[str] = []
    tag_id = tag.get("id")
    if isinstance(tag_id, str):
        parts.append(tag_id.lower())
    classes = tag.get("class") or []
    if isinstance(classes, str):
        parts.append(classes.lower())
    else:
        parts.extend(str(c).lower() for c in classes)
    return " ".join(parts)


def _is_boilerplate(tag: Tag) -> bool:
    role = tag.get("role")
    if isinstance(role, str) and role.lower() in _BOILERPLATE_ROLES:
        return True
    blob = _attr_blob(tag)
    if not blob:
        return False
    return any(token in blob for token in _BOILERPLATE_TOKENS)


def _strip_noise(soup: BeautifulSoup) -> None:
    # Collect first, then decompose — mutating while iterating leaves detached
    # tags whose attrs become None under BeautifulSoup.
    to_remove: list[Tag] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        name = (tag.name or "").lower()
        if name in _STRIP_TAGS or _is_boilerplate(tag):
            to_remove.append(tag)
    for tag in to_remove:
        tag.decompose()


def _pick_main_root(soup: BeautifulSoup) -> Tag:
    for selector in _MAIN_SELECTORS:
        found = soup.select_one(selector)
        if found is not None:
            return found
    body = soup.body
    if body is not None:
        return body
    return soup


def _title_from_soup(soup: BeautifulSoup, main: Tag) -> str:
    if soup.title and soup.title.string:
        title = normalize_text(soup.title.get_text(" ", strip=True))
        for sep in (" - GOV.UK", " | GOV.UK", " – GOV.UK"):
            if title.endswith(sep):
                title = title[: -len(sep)].strip()
                break
        if title:
            return title
    h1 = main.find("h1")
    if h1 is not None:
        return normalize_text(h1.get_text(" ", strip=True))
    return ""


def _visible_text(tag: Tag) -> str:
    return normalize_text(tag.get_text(" ", strip=True))


def _is_structural_list_container(tag: Tag) -> bool:
    """True for ul/ol used as layout shells (e.g. IND accordion), not content lists."""
    blob = _attr_blob(tag)
    return "accordion" in blob


def _extract_list(tag: Tag) -> ContentBlock | None:
    items: list[str] = []
    for li in tag.find_all("li", recursive=False):
        text = _visible_text(li)
        if text:
            items.append(text)
    if not items:
        return None
    return ContentBlock(
        type="list",
        ordered=tag.name == "ol",
        items=tuple(items),
    )


def _extract_table(tag: Tag) -> ContentBlock | None:
    caption_el = tag.find("caption")
    caption = _visible_text(caption_el) if caption_el else ""

    headers: list[str] = []
    thead = tag.find("thead")
    if thead is not None:
        header_row = thead.find("tr")
        if header_row is not None:
            for cell in header_row.find_all(["th", "td"], recursive=False):
                headers.append(_visible_text(cell))

    rows: list[tuple[str, ...]] = []
    bodies = tag.find_all("tbody", recursive=False) or [tag]
    for body in bodies:
        for tr in body.find_all("tr", recursive=False):
            cells = tr.find_all(["th", "td"], recursive=False)
            if not cells:
                continue
            if thead is None and not headers and all(c.name == "th" for c in cells):
                headers = [_visible_text(c) for c in cells]
                continue
            row = tuple(_visible_text(c) for c in cells)
            if any(row):
                rows.append(row)

    if not headers and not rows:
        return None
    return ContentBlock(
        type="table",
        caption=caption or None,
        headers=tuple(headers),
        rows=tuple(rows),
    )


def _walk(node: Tag | NavigableString, blocks: list[ContentBlock]) -> None:
    if isinstance(node, NavigableString):
        return
    if not isinstance(node, Tag):
        return
    name = (node.name or "").lower()
    if name in _STRIP_TAGS or _is_boilerplate(node):
        return

    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        text = _visible_text(node)
        if text:
            blocks.append(
                ContentBlock(type="heading", level=int(name[1]), text=text)
            )
        return

    if name == "p":
        text = _visible_text(node)
        if text:
            blocks.append(ContentBlock(type="paragraph", text=text))
        return

    if name in {"ul", "ol"}:
        if _is_structural_list_container(node):
            for child in node.children:
                _walk(child, blocks)
            return
        block = _extract_list(node)
        if block is not None:
            blocks.append(block)
        return

    if name == "table":
        block = _extract_table(node)
        if block is not None:
            blocks.append(block)
        return

    for child in node.children:
        _walk(child, blocks)


def extract_html_document(html: str | bytes) -> StructuredDocument:
    """Parse HTML bytes/text into a structured title + ordered blocks."""
    if isinstance(html, bytes):
        text = html.decode("utf-8", errors="replace")
    else:
        text = html
    soup = BeautifulSoup(text, "html.parser")
    _strip_noise(soup)
    main = _pick_main_root(soup)
    title = _title_from_soup(soup, main)
    blocks: list[ContentBlock] = []
    _walk(main, blocks)
    return StructuredDocument(title=title, blocks=blocks)


def content_blocks_as_dicts(document: StructuredDocument) -> list[dict[str, Any]]:
    return [block.to_dict() for block in document.blocks]


def structured_document_from_dict(data: dict[str, Any]) -> StructuredDocument:
    """Rebuild a StructuredDocument from stored JSON (tests / loaders)."""
    blocks: list[ContentBlock] = []
    for raw in data.get("blocks") or []:
        btype = raw.get("type")
        page = raw.get("page")
        page_i = int(page) if page is not None else None
        if btype == "heading":
            blocks.append(
                ContentBlock(
                    type="heading",
                    level=int(raw.get("level") or 1),
                    text=normalize_text(raw.get("text")),
                    page=page_i,
                )
            )
        elif btype == "paragraph":
            blocks.append(
                ContentBlock(
                    type="paragraph",
                    text=normalize_text(raw.get("text")),
                    page=page_i,
                )
            )
        elif btype == "list":
            items = tuple(
                normalize_text(item) for item in (raw.get("items") or []) if item
            )
            blocks.append(
                ContentBlock(
                    type="list",
                    ordered=bool(raw.get("ordered")),
                    items=items,
                    page=page_i,
                )
            )
        elif btype == "table":
            headers = tuple(normalize_text(h) for h in (raw.get("headers") or []))
            rows = tuple(
                tuple(normalize_text(c) for c in row)
                for row in (raw.get("rows") or [])
            )
            blocks.append(
                ContentBlock(
                    type="table",
                    caption=normalize_text(raw.get("caption")) or None,
                    headers=headers,
                    rows=rows,
                    page=page_i,
                )
            )
    empty = tuple(int(p) for p in (data.get("empty_pages") or []))
    return StructuredDocument(
        title=normalize_text(data.get("title")),
        blocks=blocks,
        page_count=data.get("page_count"),
        empty_pages=empty,
        metadata=dict(data.get("metadata") or {}),
    )


__all__ = [
    "EXTRACTOR_NAME",
    "EXTRACTOR_VERSION",
    "SCHEMA_VERSION",
    "ContentBlock",
    "StructuredDocument",
    "content_blocks_as_dicts",
    "extract_html_document",
    "normalize_text",
    "normalized_content_sha256",
    "structured_document_from_dict",
]
