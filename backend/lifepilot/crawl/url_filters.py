"""Conservative crawl-trap filters for frontier enqueue.

These heuristics reject clear traps (search loops, login walls, tracking
permutations). They intentionally do **not** reject URLs merely because a
path segment contains common guidance words such as ``events`` or ``print``
(e.g. GOV.UK ``/print`` pages or immigration event notices).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

# Exact path segment names that almost always indicate non-guidance chrome.
# Intentionally omits common guidance words such as ``events`` and ``print``.
_TRAP_SEGMENTS = frozenset(
    {
        "search",
        "search-results",
        "find",
        "login",
        "signin",
        "sign-in",
        "log-in",
        "logout",
        "signout",
        "register",
        "signup",
        "cart",
        "basket",
        "checkout",
    }
)

# Query keys that strongly indicate session/tracking loops.
_TRAP_QUERY_KEYS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "sessionid",
        "jsessionid",
        "phpsessid",
    }
)

_MAX_QUERY_KEYS = 8
_MAX_QUERY_CHARS = 256


def is_crawl_trap_url(url: str) -> tuple[bool, str]:
    """Return ``(True, reason)`` when ``url`` should not be fetched as guidance.

    Conservative: prefer false negatives (allow) over false positives (block).
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return True, "unparseable URL"

    path = (parts.path or "/").lower()
    segments = [s for s in path.split("/") if s]
    for seg in segments:
        # Exact segment match only — avoids blocking ``…/events/…`` guidance.
        if seg in _TRAP_SEGMENTS:
            return True, f"trap path segment={seg!r}"
        # Search result pages often use these compound forms.
        if seg.startswith("search-") or seg.endswith("-search"):
            return True, f"trap path segment={seg!r}"

    query = parts.query or ""
    if len(query) > _MAX_QUERY_CHARS:
        return True, "query string too long"

    try:
        pairs = parse_qsl(query, keep_blank_values=True)
    except ValueError:
        return True, "unparseable query string"

    if len(pairs) > _MAX_QUERY_KEYS:
        return True, "too many query parameters"

    keys = {k.lower() for k, _ in pairs}
    hits = keys & _TRAP_QUERY_KEYS
    if hits:
        # Tracking-only queries: reject. Mixed with real params still reject
        # tracking permutations to avoid duplicate URL explosion.
        return True, f"tracking/session query keys={sorted(hits)}"

    # Search endpoints via query without path trap (e.g. ?q=visa).
    if "q" in keys and any(s in {"search", "find", "results"} for s in segments):
        return True, "search results query loop"

    return False, ""


__all__ = ["is_crawl_trap_url"]
