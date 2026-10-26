"""Conservative host safety checks for outbound fetches."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata",
    }
)


def is_blocked_fetch_host(hostname: str | None) -> bool:
    """True for loopback/private/link-local IPs and known unsafe hostnames.

    Public DNS hostnames that are not IP literals are allowed here; crawl
    allowlists still restrict which hosts may be requested.
    """
    if not hostname:
        return True
    host = hostname.strip().lower().rstrip(".")
    if not host:
        return True
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        return True
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def url_has_blocked_fetch_host(url: str) -> bool:
    """True if the URL's hostname is blocked for outbound fetching."""
    return is_blocked_fetch_host(urlsplit(url).hostname)
