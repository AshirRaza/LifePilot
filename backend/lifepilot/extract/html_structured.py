"""Deterministic structured extraction from stored HTML pages.

Produces ordered blocks (headings, paragraphs, lists, tables) suitable for
later citation-grounded chunking. Does not fetch the network or mutate raw
files.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from lifepilot.db.temporal import (
    TemporalDateObservation,
    parse_source_datetime,
    resolve_temporal_observations,
)
from lifepilot.extract.schema import (
    SCHEMA_VERSION,
    ContentBlock,
    StructuredDocument,
    normalize_text,
    normalized_content_sha256,
)

EXTRACTOR_NAME = "html_structured"
EXTRACTOR_VERSION = "1.3.2"

# Prefer these roots when present (government sites).
_MAIN_SELECTORS = (
    "main#content",
    "main",
    "article",
    "[role='main']",
    "#content",
    "#MainContent",
    "#main-content",
    ".main-content",
    "#main",
)

# Tags removed wholesale. ``form`` is intentionally absent: many government
# sites (MOM, ASP.NET Home Affairs / DETE) wrap the entire page body in a
# form; stripping it destroyed main content. Interactive controls remain
# listed and are still removed.
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
# Avoid bare "feedback": USCIS wraps ``<main>`` in ``class="… feedback …"``.
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
    "gem-c-feedback",
    "adminfeedback",
    "feedback-message",
    "feedback-block",
    "block-adminfeedback",
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


def _main_content_anchors(soup: BeautifulSoup) -> list[Tag]:
    """Tags that identify primary page content (never strip their ancestors)."""
    anchors: list[Tag] = []
    seen: set[int] = set()
    for selector in _MAIN_SELECTORS:
        for tag in soup.select(selector):
            if not isinstance(tag, Tag):
                continue
            key = id(tag)
            if key in seen:
                continue
            seen.add(key)
            anchors.append(tag)
    return anchors


def _contains_main_anchor(tag: Tag, anchors: list[Tag]) -> bool:
    for anchor in anchors:
        if tag is anchor:
            return True
        try:
            if anchor in tag.descendants:
                return True
        except (ValueError, AttributeError):
            continue
    return False


def _strip_noise(soup: BeautifulSoup) -> None:
    # Collect first, then decompose — mutating while iterating leaves detached
    # tags whose attrs become None under BeautifulSoup.
    # Never remove a node that wraps a main-content anchor (e.g. USCIS layout
    # div with class "feedback" that also contains ``<main>``).
    anchors = _main_content_anchors(soup)
    to_remove: list[Tag] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        name = (tag.name or "").lower()
        if name not in _STRIP_TAGS and not _is_boilerplate(tag):
            continue
        if anchors and _contains_main_anchor(tag, anchors):
            continue
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
    """True for ul/ol used as layout shells, not plain content lists.

    Government pages often wrap section cards in lists (MOM modules, IND
    accordions). Those contain headings / nested blocks and must be walked,
    not collapsed into a single list block.
    """
    blob = _attr_blob(tag)
    if "accordion" in blob:
        return True
    return tag.find(["h1", "h2", "h3", "h4", "h5", "h6"]) is not None


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
    # Page-wrapping ``<form>`` elements are transparent: recurse into children
    # so ASP.NET / MOM shells do not hide headings and paragraphs.
    if name == "form":
        for child in node.children:
            _walk(child, blocks)
        return
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


_HTML_FRAGMENT_RE = re.compile(
    r"<\s*(?:p|div|ul|ol|li|h[1-6]|table|section|article|span|a|strong|em)\b",
    re.IGNORECASE,
)


def _looks_like_html_fragment(value: str) -> bool:
    if len(value) < 24:
        return False
    return bool(_HTML_FRAGMENT_RE.search(value))


def _collect_html_strings_from_json(obj: Any, out: list[str], *, depth: int = 0) -> None:
    """Collect string values that look like HTML fragments from JSON payloads."""
    if depth > 40:
        return
    if isinstance(obj, str):
        if _looks_like_html_fragment(obj):
            out.append(obj)
        return
    if isinstance(obj, dict):
        for value in obj.values():
            _collect_html_strings_from_json(value, out, depth=depth + 1)
        return
    if isinstance(obj, list):
        for value in obj:
            _collect_html_strings_from_json(value, out, depth=depth + 1)


def _title_from_embedded_json(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for key in ("title", "visaSubclassHeading", "name", "heading"):
            value = obj.get(key)
            if isinstance(value, str):
                cleaned = normalize_text(value)
                if cleaned:
                    return cleaned
        for value in obj.values():
            found = _title_from_embedded_json(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _title_from_embedded_json(value)
            if found:
                return found
    return None


def _blocks_from_json_catalogs(obj: Any) -> list[ContentBlock]:
    """Turn CMS card/tile catalogs into headings + lists/paragraphs.

    Hub pages often embed pathway tiles (title + description) or recommended
    cards (itemName) in JSON without wrapping them as HTML.
    """
    blocks: list[ContentBlock] = []
    if not isinstance(obj, dict):
        return blocks

    catalog_keys = frozenset({"tiles", "cards", "items", "links", "recommended"})

    def _iter_item_dicts(node: Any, *, in_catalog: bool = False) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if isinstance(node, list):
            if not in_catalog:
                return found
            for item in node:
                if isinstance(item, dict) and (
                    isinstance(item.get("title"), str)
                    or isinstance(item.get("itemName"), str)
                    or isinstance(item.get("name"), str)
                ):
                    found.append(item)
            return found
        if isinstance(node, dict):
            for key, value in node.items():
                key_l = str(key).lower()
                if key_l in catalog_keys or in_catalog:
                    found.extend(_iter_item_dicts(value, in_catalog=True))
        return found

    items = _iter_item_dicts(obj)
    seen: set[str] = set()
    labels: list[str] = []
    extras: list[tuple[str, str]] = []
    for item in items:
        label = normalize_text(
            item.get("title") or item.get("itemName") or item.get("name") or ""
        )
        if not label or label in seen:
            continue
        # Skip opaque machine ids like "482-69".
        if re.fullmatch(r"[\w]+-\d+", label) or re.fullmatch(r"\d+", label):
            continue
        seen.add(label)
        labels.append(label)
        desc = item.get("description")
        if isinstance(desc, str):
            desc_n = normalize_text(desc)
            if desc_n and not _looks_like_html_fragment(desc):
                extras.append((label, desc_n))

    if len(labels) >= 2:
        blocks.append(ContentBlock(type="list", ordered=False, items=tuple(labels)))
        for label, desc in extras[:20]:
            blocks.append(ContentBlock(type="heading", level=3, text=label))
            blocks.append(ContentBlock(type="paragraph", text=desc))
    return blocks


def _embedded_json_content(
    soup: BeautifulSoup,
) -> tuple[str | None, list[ContentBlock]]:
    """Recover title + blocks from embedded CMS JSON when main is empty/thin.

    Some government CMS pages leave ``<main>`` empty and store guidance as HTML
    fragments and/or card/tile catalogs inside a hidden JSON field.
    """
    title: str | None = None
    fragments: list[str] = []
    catalog: list[ContentBlock] = []
    candidates: list[str] = []

    for tag in soup.find_all(["input", "textarea"]):
        if not isinstance(tag, Tag):
            continue
        if tag.name == "input":
            input_type = (tag.get("type") or "text").lower()
            if input_type not in {"hidden", "text"}:
                continue
        raw_val = tag.get("value") if tag.name == "input" else tag.string
        if not isinstance(raw_val, str) or len(raw_val) < 80:
            continue
        candidates.append(raw_val)

    for tag in soup.find_all("script"):
        if not isinstance(tag, Tag):
            continue
        script_type = (tag.get("type") or "").lower()
        if script_type and script_type not in {
            "application/json",
            "application/ld+json",
        }:
            if "json" not in script_type:
                continue
        body = tag.string or tag.get_text() or ""
        if len(body) < 80:
            continue
        candidates.append(body)

    for raw_val in candidates:
        decoded = html_lib.unescape(raw_val).strip()
        if not decoded.startswith("{") and not decoded.startswith("["):
            continue
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and (
            "menuItems" in payload or "languagePickerItems" in payload
        ):
            continue
        html_bits: list[str] = []
        _collect_html_strings_from_json(payload, html_bits)
        catalog_blocks = _blocks_from_json_catalogs(payload)
        if not html_bits and not catalog_blocks:
            continue
        if title is None:
            title = _title_from_embedded_json(payload)
        fragments.extend(html_bits)
        if len(catalog_blocks) > len(catalog):
            catalog = catalog_blocks

    blocks = _blocks_from_html_fragments(fragments)
    if catalog and len(blocks) < 4:
        blocks = _dedupe_content_blocks(list(blocks) + catalog)
    elif catalog and not blocks:
        blocks = catalog
    return title, blocks


def _blocks_from_html_fragments(fragments: list[str]) -> list[ContentBlock]:
    if not fragments:
        return []
    # Drop fragments wholly contained in a larger fragment to avoid walking
    # the same CMS HTML twice when JSON nests parent + child snippets.
    keepers: list[str] = []
    for frag in sorted(fragments, key=len, reverse=True):
        if any(frag != kept and frag in kept for kept in keepers):
            continue
        keepers.append(frag)
    # Prefer original encounter order among survivors.
    keep_set = set(keepers)
    ordered = [frag for frag in fragments if frag in keep_set]
    wrapped = "<div id=\"embedded-guidance\">" + "\n".join(ordered) + "</div>"
    frag_soup = BeautifulSoup(wrapped, "html.parser")
    root = frag_soup.find(id="embedded-guidance")
    blocks: list[ContentBlock] = []
    if isinstance(root, Tag):
        _walk(root, blocks)
    return blocks


def _block_signature(block: ContentBlock) -> tuple[Any, ...]:
    if block.type == "list":
        return ("list", bool(block.ordered), tuple(normalize_text(i) for i in block.items))
    if block.type == "table":
        return (
            "table",
            normalize_text(block.caption),
            tuple(normalize_text(h) for h in block.headers),
            tuple(tuple(normalize_text(c) for c in row) for row in block.rows),
        )
    if block.type == "heading":
        return ("heading", block.level, normalize_text(block.text))
    return (block.type, normalize_text(block.text))


def _dedupe_content_blocks(blocks: list[ContentBlock]) -> list[ContentBlock]:
    """Drop exact duplicate blocks while preserving first-seen order.

    Embedded CMS JSON often repeats the same heading/paragraph for alternate
    actors or mirrored tabs. Exact-signature dedupe is domain-agnostic.
    """
    seen: set[tuple[Any, ...]] = set()
    out: list[ContentBlock] = []
    for block in blocks:
        sig = _block_signature(block)
        # Allow empty signatures through once only.
        if sig in seen:
            continue
        seen.add(sig)
        out.append(block)
    return out


_REQUIREMENT_GUIDANCE_MARKERS = (
    "enough money",
    "cost of living",
    "gather documents",
    "evidence you have",
    "how to apply",
    "documents required",
    "document checklist",
    "financial capacity",
    "genuine student",
    "certificate of sponsorship",
)


def _blocks_text_blob(blocks: list[ContentBlock]) -> str:
    parts: list[str] = []
    for block in blocks:
        if block.text:
            parts.append(block.text)
        if block.items:
            parts.extend(block.items)
    return " ".join(parts).lower()


def _embedded_adds_requirement_guidance(
    main_blocks: list[ContentBlock],
    embedded_blocks: list[ContentBlock],
) -> bool:
    """True when embedded CMS HTML adds requirement text absent from main DOM."""
    main_blob = _blocks_text_blob(main_blocks)
    emb_blob = _blocks_text_blob(embedded_blocks)
    if not emb_blob.strip():
        return False
    return any(
        marker in emb_blob and marker not in main_blob
        for marker in _REQUIREMENT_GUIDANCE_MARKERS
    )


_HTML_META_PUBLISHED_KEYS = frozenset(
    {
        "article:published_time",
        "og:article:published_time",
        "dcterms.issued",
        "dc.date.issued",
        "govuk:first-published-at",
        "publication_date",
        "publishdate",
        "pubdate",
        "dcterms.created",
        "dc.date",
        "date",
    }
)
_HTML_META_UPDATED_KEYS = frozenset(
    {
        "article:modified_time",
        "og:updated_time",
        "article:modified",
        "dcterms.modified",
        "dc.date.modified",
        "govuk:updated-at",
        "last-modified",
        "revised",
    }
)
_JSONLD_PUBLISHED_KEYS = ("datePublished", "dateCreated")
_JSONLD_UPDATED_KEYS = ("dateModified",)
_BANNER_DATE_CLASSES_IDS = (
    "mom-last-updated",
    "documentlastupdated",
    "gem-c-published-dates",
    "app-c-published-dates",
    "gem-c-metadata",
    "last-updated",
    "lastupdated",
    "published-date",
    "date-published",
    "date-modified",
    "modified-date",
    "page-updated",
    "node-updated",
)
_BANNER_LINE_RE = re.compile(
    r"(?i)\b(first\s+published|published(?:\s+on)?|last\s+updated|updated(?:\s+on)?|last\s+reviewed)\s*[:\-]?\s*([A-Za-z0-9,:\-+TZ/ ]{6,48})$"
)


def _walk_jsonld_dates(
    obj: Any,
    observations: list[TemporalDateObservation],
    *,
    seen: set[tuple[str, str, str]],
    depth: int = 0,
) -> None:
    if depth > 10:
        return
    if isinstance(obj, list):
        for item in obj:
            _walk_jsonld_dates(item, observations, seen=seen, depth=depth + 1)
        return
    if not isinstance(obj, dict):
        return
    for key, kind in (
        *((k, "published") for k in _JSONLD_PUBLISHED_KEYS),
        *((k, "updated") for k in _JSONLD_UPDATED_KEYS),
    ):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            raw = val.strip()
            sig = ("html_json_ld", f"jsonld:{key}", raw)
            if sig in seen:
                continue
            seen.add(sig)
            dt, err, status = parse_source_datetime(raw)
            observations.append(
                TemporalDateObservation(
                    kind=kind,
                    extraction_method="html_json_ld",
                    source_field=f"jsonld:{key}",
                    raw_value=raw,
                    normalized_utc=dt.isoformat() if dt else None,
                    status=status if dt else (err or "malformed"),
                    reliability="structured_high" if dt else "unparsed",
                    notes=err,
                )
            )
    for value in obj.values():
        if isinstance(value, (dict, list)):
            _walk_jsonld_dates(value, observations, seen=seen, depth=depth + 1)


def extract_html_temporal_metadata(html_or_soup: str | bytes | BeautifulSoup) -> dict[str, Any]:
    """Extract observed publication/update dates and provenance from raw HTML.

    Never parses policy prose for dates and never treats observed dates as legal
    effective dates.
    """
    if isinstance(html_or_soup, BeautifulSoup):
        soup = html_or_soup
    else:
        text = (
            html_or_soup.decode("utf-8", errors="replace")
            if isinstance(html_or_soup, bytes)
            else html_or_soup
        )
        soup = BeautifulSoup(text, "html.parser")

    observations: list[TemporalDateObservation] = []
    seen: set[tuple[str, str, str]] = set()

    # 1. JSON-LD blocks
    for tag in soup.find_all("script"):
        if not isinstance(tag, Tag):
            continue
        stype = (tag.get("type") or "").strip().lower()
        if stype != "application/ld+json":
            continue
        body = (tag.string or tag.get_text() or "").strip()
        if not body or len(body) > 500_000:
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        _walk_jsonld_dates(payload, observations, seen=seen)

    # 2. Standard HTML <meta> tags
    for tag in soup.find_all("meta"):
        if not isinstance(tag, Tag):
            continue
        content = tag.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        raw = content.strip()
        key = (
            tag.get("property") or tag.get("name") or tag.get("http-equiv") or ""
        ).strip().lower()
        if not key:
            continue
        kind: str | None = None
        if key in _HTML_META_PUBLISHED_KEYS:
            kind = "published"
        elif key in _HTML_META_UPDATED_KEYS:
            kind = "updated"
        if kind is None:
            continue
        field_name = f"meta:{key}"
        sig = ("html_meta", field_name, raw)
        if sig in seen:
            continue
        seen.add(sig)
        dt, err, status = parse_source_datetime(raw)
        observations.append(
            TemporalDateObservation(
                kind=kind,
                extraction_method="html_meta",
                source_field=field_name,
                raw_value=raw,
                normalized_utc=dt.isoformat() if dt else None,
                status=status if dt else (err or "malformed"),
                reliability="meta_medium" if dt else "unparsed",
                notes=err,
            )
        )

    # 3. <time datetime="..."> elements explicitly labeled as published/modified
    for tag in soup.find_all("time"):
        if not isinstance(tag, Tag):
            continue
        raw_dt = tag.get("datetime")
        if not isinstance(raw_dt, str) or not raw_dt.strip():
            continue
        raw = raw_dt.strip()
        itemprop = str(tag.get("itemprop") or "").strip().lower()
        prop = str(tag.get("property") or "").strip().lower()
        cls_blob = " ".join(str(c).lower() for c in (tag.get("class") or []))
        marker = f"{itemprop} {prop} {cls_blob}"
        kind = None
        if any(k in marker for k in ("datepublished", "published", "pubdate")):
            kind = "published"
        elif any(k in marker for k in ("datemodified", "updated", "modified")):
            kind = "updated"
        if kind is None:
            continue
        field_name = f"time:{itemprop or prop or 'class'}"
        sig = ("html_time_element", field_name, raw)
        if sig in seen:
            continue
        seen.add(sig)
        dt, err, status = parse_source_datetime(raw)
        observations.append(
            TemporalDateObservation(
                kind=kind,
                extraction_method="html_time_element",
                source_field=field_name,
                raw_value=raw,
                normalized_utc=dt.isoformat() if dt else None,
                status=status if dt else (err or "malformed"),
                reliability="meta_medium" if dt else "unparsed",
                notes=err,
            )
        )

    # 4. Explicit government date banners (MOM, GOV.UK, labeled page metadata)
    for tag in soup.find_all(["span", "div", "p", "dl", "section"]):
        if not isinstance(tag, Tag):
            continue
        blob = _attr_blob(tag)
        if not blob or not any(tok in blob for tok in _BANNER_DATE_CLASSES_IDS):
            continue
        txt = normalize_text(tag.get_text(" ", strip=True))
        if not txt or len(txt) > 220:
            continue
        for seg in re.split(r"[|;\n]+", txt):
            seg = seg.strip()
            m = _BANNER_LINE_RE.search(seg)
            if not m:
                continue
            label = m.group(1).lower()
            raw = m.group(2).strip(" .")
            kind = "published" if "published" in label else "updated"
            field_name = f"banner:{label}"
            sig = ("html_gov_banner", field_name, raw)
            if sig in seen:
                continue
            seen.add(sig)
            dt, err, status = parse_source_datetime(raw)
            observations.append(
                TemporalDateObservation(
                    kind=kind,
                    extraction_method="html_gov_banner",
                    source_field=field_name,
                    raw_value=raw,
                    normalized_utc=dt.isoformat() if dt else None,
                    status=status if dt else (err or "malformed"),
                    reliability="banner_medium" if dt else "unparsed",
                    notes=err,
                )
            )

    if not observations:
        return {}
    return resolve_temporal_observations(observations).to_storage_dict()


def extract_html_language(soup: BeautifulSoup) -> str | None:
    """Extract declared page language from <html lang>, meta content-language, or og:locale."""
    from lifepilot.agent.provenance import normalize_language_tag

    html_tag = soup.find("html")
    if isinstance(html_tag, Tag):
        raw_lang = html_tag.get("lang") or html_tag.get("xml:lang")
        if isinstance(raw_lang, str):
            norm = normalize_language_tag(raw_lang)
            if norm:
                return norm
    for meta in soup.find_all("meta"):
        if not isinstance(meta, Tag):
            continue
        heq = str(meta.get("http-equiv") or "").strip().lower()
        name = str(meta.get("name") or "").strip().lower()
        prop = str(meta.get("property") or "").strip().lower()
        if heq == "content-language" or name in {"language", "dc.language", "dcterms.language"} or prop == "og:locale":
            raw = meta.get("content")
            if isinstance(raw, str):
                first = raw.split(",")[0].strip()
                norm = normalize_language_tag(first)
                if norm:
                    return norm
    return None


def extract_html_language_from_html(html: str | bytes) -> str | None:
    """Parse raw HTML text/bytes and return normalized declared language or None."""
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else (html or "")
    if not text.strip():
        return None
    soup = BeautifulSoup(text, "html.parser")
    return extract_html_language(soup)


def extract_html_document(html: str | bytes) -> StructuredDocument:
    """Parse HTML bytes/text into a structured title + ordered blocks."""
    if isinstance(html, bytes):
        text = html.decode("utf-8", errors="replace")
    else:
        text = html
    # Keep an unstripped copy for embedded JSON recovery and temporal metadata
    # (meta/banners may sit inside blocks stripped by noise removal).
    soup_raw = BeautifulSoup(text, "html.parser")
    temporal_metadata = extract_html_temporal_metadata(soup_raw)
    language = extract_html_language(soup_raw)
    soup = BeautifulSoup(text, "html.parser")
    _strip_noise(soup)
    main = _pick_main_root(soup)
    title = _title_from_soup(soup, main)
    blocks: list[ContentBlock] = []
    _walk(main, blocks)

    # Embedded CMS JSON recovery (AU PageSchema and similar):
    # - thin/empty main → prefer embedded blocks
    # - non-thin main → merge embedded fragments when they add requirement
    #   guidance missing from DOM (e.g. financial capacity / gather documents)
    main_text_len = len(_visible_text(main)) if isinstance(main, Tag) else 0
    embedded_title, embedded_blocks = _embedded_json_content(soup_raw)
    if embedded_blocks:
        if len(blocks) < 4 or main_text_len < 200:
            if len(embedded_blocks) > len(blocks):
                blocks = embedded_blocks
                if embedded_title and (
                    not title or title.lower() in {"", "home", "overview"}
                ):
                    title = embedded_title
        elif _embedded_adds_requirement_guidance(blocks, embedded_blocks):
            blocks = _dedupe_content_blocks(list(blocks) + list(embedded_blocks))
            if embedded_title and (
                not title or title.lower() in {"", "home", "overview"}
            ):
                title = embedded_title

    blocks = _dedupe_content_blocks(blocks)
    return StructuredDocument(
        title=title,
        blocks=blocks,
        temporal_metadata=temporal_metadata,
        language=language,
    )


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
        temporal_metadata=dict(data.get("temporal_metadata") or {}),
    )


__all__ = [
    "EXTRACTOR_NAME",
    "EXTRACTOR_VERSION",
    "SCHEMA_VERSION",
    "ContentBlock",
    "StructuredDocument",
    "content_blocks_as_dicts",
    "extract_html_document",
    "extract_html_temporal_metadata",
    "normalize_text",
    "normalized_content_sha256",
    "structured_document_from_dict",
]
