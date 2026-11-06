"""Database URL helpers."""

from __future__ import annotations

from sqlalchemy.engine import URL, make_url

# On Windows, ``localhost`` often resolves to IPv6 ``::1`` first while Docker
# Postgres is bound to IPv4 ``127.0.0.1`` only — causing multi-second delays or
# apparent hangs. Prefer the IPv4 loopback explicitly.
_LOOPBACK_HOSTS = frozenset({"localhost", "::1"})


def normalize_database_url(url: str | URL) -> str:
    """Return a SQLAlchemy URL string with loopback hosts mapped to 127.0.0.1."""
    parsed = make_url(url)
    host = (parsed.host or "").lower()
    if host in _LOOPBACK_HOSTS:
        parsed = parsed.set(host="127.0.0.1")
    return parsed.render_as_string(hide_password=False)
