"""Redact secrets and credential-like values from logs and API errors."""

from __future__ import annotations

import re
from typing import Any, Mapping

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[a-z0-9\-._~+/]+=*"),
    re.compile(r"(?i)sk-ant-[a-z0-9\-_]{8,}"),
    re.compile(r"(?i)postgresql(\+psycopg)?://[^\s\"']+"),
]


def redact_secrets(text: str | None) -> str:
    if not text:
        return ""
    out = str(text)
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def redact_mapping(data: Mapping[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {}
    sensitive = {
        "password",
        "api_key",
        "apikey",
        "token",
        "secret",
        "authorization",
        "database_url",
        "anthropic_api_key",
    }
    out: dict[str, Any] = {}
    for key, value in data.items():
        if str(key).lower() in sensitive:
            out[key] = "[REDACTED]"
        elif isinstance(value, str):
            out[key] = redact_secrets(value)
        else:
            out[key] = value
    return out
