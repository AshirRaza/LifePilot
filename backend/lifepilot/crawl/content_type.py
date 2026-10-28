"""Shared HTTP Content-Type helpers."""

from __future__ import annotations


def content_type_base(value: str | None) -> str | None:
    """Return the MIME type without parameters (lowercased)."""
    if not value:
        return None
    return value.split(";", 1)[0].strip().lower()
