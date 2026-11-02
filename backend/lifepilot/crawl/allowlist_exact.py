"""Enqueue explicitly approved exact document URLs from a source allowlist.

Wildcard patterns (ending in ``*``) are never treated as fetchable URLs.
Only literal HTTPS document URLs listed in ``allowed_url_patterns`` are
eligible — used when a seed page links equivalents outside scope (e.g. NL
form PDFs under ``/nl/formulieren/`` while ``/en/forms/5013.pdf`` is approved).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from lifepilot.crawl.frontier import EnqueueResult, enqueue_url
from lifepilot.db.models import Source
from lifepilot.registry.urls import (
    InvalidRegistryUrlError,
    normalize_url,
    parse_allowed_pattern,
)


@dataclass(frozen=True)
class ExactAllowlistUrl:
    pattern: str
    url_normalized: str
    content_kind_hint: str


def _kind_hint(url: str) -> str:
    path = (urlsplit(url).path or "").lower()
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith(".docx"):
        return "docx"
    return "html_page"


def iter_exact_allowlist_urls(
    patterns: list[str] | tuple[str, ...] | None,
) -> list[ExactAllowlistUrl]:
    """Return literal allowlist entries that are concrete document URLs.

    Recursive/wildcard patterns (``…*``) are excluded. Malformed patterns are
    skipped rather than raised so crawl startup stays resilient.
    """
    out: list[ExactAllowlistUrl] = []
    seen: set[str] = set()
    for raw in patterns or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        try:
            parsed = parse_allowed_pattern(text)
        except InvalidRegistryUrlError:
            continue
        if parsed.recursive:
            continue
        try:
            # Non-recursive patterns are exact URLs (possibly with trailing slash
            # normalized away).
            candidate = normalize_url(text)
        except InvalidRegistryUrlError:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(
            ExactAllowlistUrl(
                pattern=text,
                url_normalized=candidate,
                content_kind_hint=_kind_hint(candidate),
            )
        )
    return out


def enqueue_exact_allowlist_urls(
    session: Session,
    source: Source,
    *,
    enqueue_budget: list[int] | None = None,
) -> list[EnqueueResult]:
    """Idempotently enqueue exact allowlisted document URLs at depth 0."""
    results: list[EnqueueResult] = []
    seed_norm = source.seed_url_normalized or ""
    for item in iter_exact_allowlist_urls(source.allowed_url_patterns):
        if item.url_normalized == seed_norm:
            continue
        if enqueue_budget is not None and enqueue_budget[0] <= 0:
            break
        result = enqueue_url(
            session,
            source,
            url=item.url_normalized,
            discovery_method="allowlist_exact",
            depth=0,
            parent_url=seed_norm or None,
            content_kind_hint=item.content_kind_hint,
            apply_trap_filter=False,
        )
        results.append(result)
        if (
            enqueue_budget is not None
            and result.outcome == "inserted_pending"
        ):
            enqueue_budget[0] = max(0, enqueue_budget[0] - 1)
    return results


__all__ = [
    "ExactAllowlistUrl",
    "enqueue_exact_allowlist_urls",
    "iter_exact_allowlist_urls",
]
