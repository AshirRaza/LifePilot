"""Load configuration-driven registry seed definitions from JSON."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lifepilot.registry.seeds import AuthoritySeed, JurisdictionSeed, SourceSeed

_ISO_ALPHA2 = re.compile(r"^[A-Z]{2}$")
_JURISDICTION_TYPES = frozenset(
    {
        "supranational",
        "country",
        "state",
        "province",
        "region",
        "municipality",
        "other",
    }
)


class RegistryConfigError(ValueError):
    """Invalid registry configuration file."""


@dataclass(frozen=True)
class RegistryConfig:
    jurisdictions: tuple[JurisdictionSeed, ...]
    authorities: tuple[AuthoritySeed, ...]
    sources: tuple[SourceSeed, ...]
    path: str | None = None


def _require_str(obj: dict[str, Any], key: str, *, ctx: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryConfigError(f"{ctx}: missing/invalid string field {key!r}")
    return value.strip()


def _optional_str(obj: dict[str, Any], key: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RegistryConfigError(f"field {key!r} must be a string or null")
    return value.strip() or None


def _parse_topics(raw: Any, *, ctx: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise RegistryConfigError(f"{ctx}: topics must be a non-empty list")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise RegistryConfigError(f"{ctx}: topics entries must be non-empty strings")
        out.append(item.strip())
    return tuple(out)


def _parse_hosts(raw: Any, *, ctx: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise RegistryConfigError(f"{ctx}: expected_hosts must be a non-empty list")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise RegistryConfigError(
                f"{ctx}: expected_hosts entries must be non-empty strings"
            )
        out.append(item.strip().lower())
    return tuple(out)


def load_registry_config(path: Path | str) -> RegistryConfig:
    """Parse and validate a JSON registry config file."""
    config_path = Path(path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryConfigError(f"config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryConfigError(f"invalid JSON in {config_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise RegistryConfigError("config root must be an object")

    raw_jurisdictions = payload.get("jurisdictions")
    raw_authorities = payload.get("authorities")
    raw_sources = payload.get("sources")
    if not isinstance(raw_jurisdictions, list) or not raw_jurisdictions:
        raise RegistryConfigError("jurisdictions must be a non-empty list")
    if not isinstance(raw_authorities, list) or not raw_authorities:
        raise RegistryConfigError("authorities must be a non-empty list")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise RegistryConfigError("sources must be a non-empty list")

    jurisdictions: list[JurisdictionSeed] = []
    seen_codes: set[str] = set()
    for item in raw_jurisdictions:
        if not isinstance(item, dict):
            raise RegistryConfigError("jurisdiction entries must be objects")
        code = _require_str(item, "code", ctx="jurisdiction").upper()
        if code in seen_codes:
            raise RegistryConfigError(f"duplicate jurisdiction code {code!r}")
        seen_codes.add(code)
        jtype = _require_str(item, "jurisdiction_type", ctx=f"jurisdiction {code}")
        if jtype not in _JURISDICTION_TYPES:
            raise RegistryConfigError(
                f"jurisdiction {code}: invalid jurisdiction_type {jtype!r}"
            )
        country_code = _optional_str(item, "country_code")
        if country_code is not None:
            country_code = country_code.upper()
            if not _ISO_ALPHA2.match(country_code):
                raise RegistryConfigError(
                    f"jurisdiction {code}: country_code must be ISO 3166-1 alpha-2"
                )
        if jtype == "country" and country_code is None:
            raise RegistryConfigError(
                f"jurisdiction {code}: country jurisdictions require country_code"
            )
        jurisdictions.append(
            JurisdictionSeed(
                code=code,
                name=_require_str(item, "name", ctx=f"jurisdiction {code}"),
                jurisdiction_type=jtype,
                country_code=country_code,
            )
        )

    authorities: list[AuthoritySeed] = []
    seen_auth_keys: set[str] = set()
    jur_codes = {j.code for j in jurisdictions}
    for item in raw_authorities:
        if not isinstance(item, dict):
            raise RegistryConfigError("authority entries must be objects")
        key = _require_str(item, "key", ctx="authority")
        if key in seen_auth_keys:
            raise RegistryConfigError(f"duplicate authority key {key!r}")
        seen_auth_keys.add(key)
        jcode = _require_str(item, "jurisdiction_code", ctx=f"authority {key}").upper()
        if jcode not in jur_codes:
            raise RegistryConfigError(
                f"authority {key}: unknown jurisdiction_code {jcode!r}"
            )
        authorities.append(
            AuthoritySeed(
                key=key,
                jurisdiction_code=jcode,
                name=_require_str(item, "name", ctx=f"authority {key}"),
                short_name=_optional_str(item, "short_name"),
                website_url=_optional_str(item, "website_url"),
            )
        )

    sources: list[SourceSeed] = []
    seen_source_keys: set[str] = set()
    auth_keys = {a.key for a in authorities}
    for item in raw_sources:
        if not isinstance(item, dict):
            raise RegistryConfigError("source entries must be objects")
        key = _require_str(item, "key", ctx="source")
        if key in seen_source_keys:
            raise RegistryConfigError(f"duplicate source key {key!r}")
        seen_source_keys.add(key)
        jcode = _require_str(item, "jurisdiction_code", ctx=f"source {key}").upper()
        if jcode not in jur_codes:
            raise RegistryConfigError(
                f"source {key}: unknown jurisdiction_code {jcode!r}"
            )
        akey = _require_str(item, "authority_key", ctx=f"source {key}")
        if akey not in auth_keys:
            raise RegistryConfigError(f"source {key}: unknown authority_key {akey!r}")
        extra_patterns = item.get("extra_allowed_patterns") or []
        if not isinstance(extra_patterns, list):
            raise RegistryConfigError(
                f"source {key}: extra_allowed_patterns must be a list"
            )
        for pattern in extra_patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                raise RegistryConfigError(
                    f"source {key}: extra_allowed_patterns entries must be strings"
                )
        notes_parts: list[str] = []
        source_type = _optional_str(item, "source_type")
        if source_type:
            notes_parts.append(f"source_type={source_type}")
        extra = _optional_str(item, "extra_notes") or ""
        if notes_parts and extra:
            extra = " | ".join(notes_parts) + " | " + extra
        elif notes_parts:
            extra = " | ".join(notes_parts)
        sources.append(
            SourceSeed(
                key=key,
                jurisdiction_code=jcode,
                authority_key=akey,
                name=_require_str(item, "name", ctx=f"source {key}"),
                seed_url=_require_str(item, "seed_url", ctx=f"source {key}"),
                language=_require_str(item, "language", ctx=f"source {key}"),
                topics=_parse_topics(item.get("topics"), ctx=f"source {key}"),
                expected_hosts=_parse_hosts(
                    item.get("expected_hosts"), ctx=f"source {key}"
                ),
                extra_notes=extra,
                extra_allowed_patterns=tuple(
                    p.strip() for p in extra_patterns if isinstance(p, str)
                ),
            )
        )

    return RegistryConfig(
        jurisdictions=tuple(jurisdictions),
        authorities=tuple(authorities),
        sources=tuple(sources),
        path=str(config_path),
    )
