"""Discover navigable URLs embedded in CMS PageSchema / hub JSON.

Home Affairs (and similar) pages leave ``<main>`` empty and store pathway
tiles / guidance HTML inside a hidden JSON field. Static ``<a href>``
discovery therefore finds no in-scope children.

This module extracts only genuine navigation targets:
- Explicit link-like object keys (``link``, ``url``, ``href``, …)
- ``href`` attributes inside HTML fragment strings

It does not scrape arbitrary path-like text from unrelated JSON values.
Allowlist filtering is applied by the caller via ``resolve_and_classify_link``.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import Tag

from lifepilot.crawl.links import ExtractedLink, resolve_and_classify_link

_LINK_KEYS = frozenset(
    {
        "link",
        "url",
        "href",
        "path",
        "permalink",
        "canonicalurl",
        "canonical_url",
        "targeturl",
        "target_url",
    }
)

_HREF_ATTR_RE = re.compile(
    r"""href\s*=\s*(?:(['"])(.*?)\1|([^\s>]+))""",
    re.IGNORECASE | re.DOTALL,
)

_HTML_HINT_RE = re.compile(
    r"<\s*(?:a|p|div|ul|ol|li|h[1-6]|span|section)\b",
    re.IGNORECASE,
)


def _is_plausible_nav_href(value: str) -> bool:
    href = value.strip()
    if not href or href in {"#", "/"}:
        return False
    lower = href.lower()
    if lower.startswith(("javascript:", "mailto:", "tel:", "data:")):
        return False
    if lower.startswith("#") and "/" not in href:
        return False
    # Opaque CMS card ids are not URLs.
    if re.fullmatch(r"[\w.-]+-\d+", href) or re.fullmatch(r"\d+", href):
        return False
    return True


def _collect_nav_hrefs_from_json(obj: Any, out: list[str], *, depth: int = 0) -> None:
    if depth > 40:
        return
    if isinstance(obj, str):
        if _HTML_HINT_RE.search(obj):
            for match in _HREF_ATTR_RE.finditer(obj):
                href = match.group(2) if match.group(2) is not None else match.group(3)
                if href and _is_plausible_nav_href(href):
                    out.append(html_lib.unescape(href.strip()))
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).lower()
            if key_l in _LINK_KEYS and isinstance(value, str):
                if _is_plausible_nav_href(value):
                    out.append(html_lib.unescape(value.strip()))
            _collect_nav_hrefs_from_json(value, out, depth=depth + 1)
        return
    if isinstance(obj, list):
        for item in obj:
            _collect_nav_hrefs_from_json(item, out, depth=depth + 1)


def extract_pageschema_payloads(html: str | bytes) -> list[Any]:
    """Return parsed JSON payloads that may contain page navigation schema."""
    if isinstance(html, bytes):
        text = html.decode("utf-8", errors="replace")
    else:
        text = html
    soup = BeautifulSoup(text, "html.parser")
    payloads: list[Any] = []

    for tag in soup.find_all(["input", "textarea"]):
        if not isinstance(tag, Tag):
            continue
        if tag.name == "input":
            input_type = (tag.get("type") or "text").lower()
            if input_type not in {"hidden", "text"}:
                continue
        raw_val = tag.get("value") if tag.name == "input" else tag.string
        if not isinstance(raw_val, str) or len(raw_val) < 40:
            continue
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
            # Site-wide SharePoint nav blob — not page guidance.
            continue
        payloads.append(payload)

    return payloads


def extract_raw_pageschema_hrefs(html: str | bytes) -> list[str]:
    """Deduplicated raw href/link values from PageSchema-like JSON in HTML."""
    seen: set[str] = set()
    out: list[str] = []
    for payload in extract_pageschema_payloads(html):
        found: list[str] = []
        _collect_nav_hrefs_from_json(payload, found)
        for href in found:
            if href in seen:
                continue
            seen.add(href)
            out.append(href)
    return out


def discover_links_from_pageschema(
    html: str | bytes,
    *,
    base_url: str,
    allowed_patterns: list[str],
) -> list[ExtractedLink]:
    """Classify PageSchema navigation URLs against the source allowlist."""
    seen: set[str] = set()
    results: list[ExtractedLink] = []
    for href in extract_raw_pageschema_hrefs(html):
        link = resolve_and_classify_link(
            href,
            base_url=base_url,
            allowed_patterns=allowed_patterns,
        )
        key = link.url_normalized or f"{link.kind}:{link.raw_href}"
        if key in seen:
            continue
        seen.add(key)
        results.append(link)
    return results


__all__ = [
    "discover_links_from_pageschema",
    "extract_pageschema_payloads",
    "extract_raw_pageschema_hrefs",
]
