"""Extract and filter hyperlinks from stored HTML (bounded discovery)."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

from lifepilot.registry.urls import (
    InvalidRegistryUrlError,
    normalize_url,
    url_in_any_allowed_pattern,
)


@dataclass(frozen=True)
class ExtractedLink:
    """A candidate URL found in HTML before/after scope filtering."""

    raw_href: str
    absolute_url: str
    url_normalized: str | None
    kind: str  # in_scope_html | in_scope_pdf | in_scope_other | out_of_scope | invalid
    reason: str = ""


class _HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value.strip())


def extract_raw_hrefs(html: str) -> list[str]:
    """Return raw href attribute values from anchor tags."""
    parser = _HrefCollector()
    parser.feed(html)
    parser.close()
    return parser.hrefs


def _classify_path(url: str) -> str:
    path = (urlsplit(url).path or "").lower()
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith((".html", ".htm", ".xhtml")) or path.endswith("/"):
        return "html"
    # Extension-less government paths are usually HTML pages.
    if "." not in path.rsplit("/", 1)[-1]:
        return "html"
    if path.endswith((".doc", ".docx", ".xls", ".xlsx", ".zip")):
        return "other"
    return "html"


def resolve_and_classify_link(
    href: str,
    *,
    base_url: str,
    allowed_patterns: list[str],
) -> ExtractedLink:
    """Resolve one href against base_url and classify by allowlist + type."""
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return ExtractedLink(
            raw_href=href,
            absolute_url="",
            url_normalized=None,
            kind="invalid",
            reason="unsupported or empty href",
        )

    absolute = urljoin(base_url, href)
    absolute, _frag = urldefrag(absolute)
    try:
        normalized = normalize_url(absolute)
    except InvalidRegistryUrlError as exc:
        return ExtractedLink(
            raw_href=href,
            absolute_url=absolute,
            url_normalized=None,
            kind="invalid",
            reason=str(exc),
        )

    if not url_in_any_allowed_pattern(normalized, allowed_patterns):
        return ExtractedLink(
            raw_href=href,
            absolute_url=absolute,
            url_normalized=normalized,
            kind="out_of_scope",
            reason="outside approved allowlist",
        )

    path_kind = _classify_path(normalized)
    if path_kind == "pdf":
        kind = "in_scope_pdf"
    elif path_kind == "other":
        kind = "in_scope_other"
    else:
        kind = "in_scope_html"

    return ExtractedLink(
        raw_href=href,
        absolute_url=absolute,
        url_normalized=normalized,
        kind=kind,
        reason="within approved allowlist",
    )


def discover_links_from_html(
    html: str,
    *,
    base_url: str,
    allowed_patterns: list[str],
) -> list[ExtractedLink]:
    """Extract unique in-document links and classify each one."""
    seen: set[str] = set()
    results: list[ExtractedLink] = []
    for href in extract_raw_hrefs(html):
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
