"""Controlled source-authority taxonomy, issuing authority, and locale helpers (Phase 10C.2).

Separates issuing authority from document category. Never fabricates unknown
authority classifications, legal statuses, or number-format locales from
jurisdiction alone.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from lifepilot.coverage.corpus_reliability.pathway_applicability import (
    is_generic_guidance_url,
)


class SourceAuthorityCategory(str, Enum):
    """Controlled document/source authority classification (not a blind hierarchy)."""

    PRIMARY_LEGISLATION = "primary_legislation"
    OFFICIAL_BINDING_RULES = "official_binding_rules"
    OFFICIAL_GUIDANCE = "official_guidance"
    OFFICIAL_NOTICE = "official_notice"
    GOVERNMENT_PORTAL = "government_portal"
    GOVERNMENT_STATISTICAL_DATASET = "government_statistical_dataset"
    OTHER_OFFICIAL_SOURCE = "other_official_source"
    UNVERIFIED_EXTERNAL = "unverified_external"
    UNKNOWN = "unknown"


NON_NORMATIVE_AUTHORITY_CATEGORIES = frozenset(
    {
        SourceAuthorityCategory.GOVERNMENT_STATISTICAL_DATASET.value,
        SourceAuthorityCategory.GOVERNMENT_PORTAL.value,
    }
)

_EU_LOCALE_LANGS = frozenset({"nl", "de", "fr", "es", "it", "pt", "pl"})
_ANGLO_REGIONAL_LOCALES = frozenset(
    {"en-GB", "en-US", "en-IE", "en-CA", "en-AU", "en-NZ", "en-SG"}
)

_LEGISLATION_HOST_HINTS = (
    "eur-lex.europa.eu",
    "publications.europa.eu",
    "legislation.gov.uk",
    "federalregister.gov",
)

_OFFICIAL_HOST_AUTHORITIES: tuple[tuple[str, str], ...] = (
    ("gov.uk", "UK Government (GOV.UK)"),
    ("ind.nl", "IND (Immigration and Naturalisation Service)"),
    ("enterprise.gov.ie", "DETE (Department of Enterprise, Trade and Employment)"),
    ("irishimmigration.ie", "ISD (Immigration Service Delivery Ireland)"),
    ("mom.gov.sg", "MOM (Ministry of Manpower)"),
    ("ica.gov.sg", "ICA (Immigration & Checkpoints Authority)"),
    ("uscis.gov", "USCIS (U.S. Citizenship and Immigration Services)"),
    ("travel.state.gov", "U.S. Department of State"),
    ("federalregister.gov", "U.S. Federal Register"),
    ("canada.ca", "Government of Canada"),
    ("open.canada.ca", "Open Government Canada"),
    ("homeaffairs.gov.au", "Australian Department of Home Affairs"),
    ("immigration.govt.nz", "Immigration New Zealand"),
    ("service-public.gouv.fr", "Service-Public (French Republic)"),
    ("service-public.fr", "Service-Public (French Republic)"),
    ("france-visas.gouv.fr", "France-Visas"),
    ("bamf.de", "BAMF (Federal Office for Migration and Refugees)"),
    ("auswaertiges-amt.de", "German Federal Foreign Office"),
    ("make-it-in-germany.com", "Make it in Germany (Federal Portal)"),
    ("sem.admin.ch", "SEM (Swiss State Secretariat for Migration)"),
    ("eur-lex.europa.eu", "European Union (EUR-Lex)"),
    ("publications.europa.eu", "Publications Office of the European Union"),
    ("data.europa.eu", "data.europa.eu"),
    ("home-affairs.ec.europa.eu", "European Commission"),
    ("europa.eu", "European Union"),
    ("isa.go.jp", "Immigration Services Agency of Japan"),
    ("moj.go.jp", "Ministry of Justice of Japan"),
    ("u.ae", "UAE Government Portal"),
    ("icp.gov.ae", "UAE ICP"),
)


def normalize_language_tag(raw: str | None) -> str | None:
    """Normalize a BCP-47 language tag (e.g. 'en_gb' -> 'en-GB'); None if invalid."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip().replace("_", "-")
    if not cleaned:
        return None
    low = cleaned.lower()
    if low in {"und", "zxx", "unknown", "none", "null"}:
        return None
    m = re.fullmatch(r"([a-z]{2,3})(?:-([a-z]{2}))?(?:-[a-z0-9]{2,8})?", low)
    if not m:
        return None
    lang = m.group(1)
    region = m.group(2)
    return f"{lang}-{region.upper()}" if region else lang


def locale_hint_from_language(language: str | None) -> str | None:
    """Map an explicit document language/locale to numeric parser locale_hint.

    - Distinguishes plain language ('en' -> None, keeps '38.000' ambiguous) from
      regional/continental number locales ('en-GB' -> 'gb', 'nl'/'de'/'fr' -> 'eu').
    - Never infers locale from jurisdiction alone.
    """
    norm = normalize_language_tag(language)
    if not norm:
        return None
    if norm in _ANGLO_REGIONAL_LOCALES:
        return "gb"
    primary = norm.split("-", 1)[0].lower()
    if primary in _EU_LOCALE_LANGS:
        return "eu"
    return None


def host_authority_label(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlsplit(url.strip()).hostname or "").lower()
    if not host:
        return None
    for suffix, label in _OFFICIAL_HOST_AUTHORITIES:
        if host == suffix or host.endswith(f".{suffix}"):
            return label
    return None


def infer_issuing_authority(
    url: str | None,
    *,
    authority_name: str | None = None,
    source_name: str | None = None,
    provider: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """Resolve issuing organization cleanly without fabricating unknown authorities."""
    if authority_name and authority_name.strip():
        return authority_name.strip()
    meta = metadata if isinstance(metadata, dict) else {}
    for key in ("issuing_authority", "organization", "publisher", "catalog"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            title = val.get("title") or val.get("name")
            if isinstance(title, str) and title.strip():
                return title.strip()
    for list_key in ("agencies", "organisations"):
        vals = meta.get(list_key)
        if isinstance(vals, list) and vals:
            names = [str(v).strip() for v in vals if isinstance(v, str) and v.strip()]
            if names:
                return ", ".join(dict.fromkeys(names))
    host_label = host_authority_label(url)
    if host_label:
        return host_label
    if provider == "eurlex_cellar":
        return "European Union (EUR-Lex)"
    if provider == "eu_immigration_portal":
        return "European Commission"
    if provider == "govuk_content":
        return "UK Government (GOV.UK)"
    if source_name and source_name.strip():
        return source_name.strip()
    return None


def classify_source_authority(
    *,
    source_type: str,
    source_url: str | None = None,
    provider: str | None = None,
    external_source_type: str | None = None,
    legal_status: str | None = None,
    authority_name: str | None = None,
    source_name: str | None = None,
    authority_id: int | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[SourceAuthorityCategory, str | None]:
    """Classify document category and issuing authority without unsupported assumptions."""
    issuing = infer_issuing_authority(
        source_url,
        authority_name=authority_name,
        source_name=source_name,
        provider=provider,
        metadata=metadata,
    )
    stype = (source_type or "").strip().lower()
    ext_type = (external_source_type or "").strip().lower()
    lstat = (legal_status or "").strip().lower()
    url = (source_url or "").strip()
    host = (urlsplit(url).hostname or "").lower() if url else ""

    if stype == "curated_rule":
        return SourceAuthorityCategory.OFFICIAL_BINDING_RULES, issuing or "Curated LegalRule"

    if stype == "external_api":
        if ext_type == "dataset" or lstat == "dataset_catalog":
            return SourceAuthorityCategory.GOVERNMENT_STATISTICAL_DATASET, issuing
        if (
            ext_type == "proposed_rule"
            or lstat in {"proposed", "proposed_not_effective", "proposed_or_preparatory"}
        ):
            return SourceAuthorityCategory.OFFICIAL_NOTICE, issuing
        if ext_type == "final_rule":
            return SourceAuthorityCategory.OFFICIAL_BINDING_RULES, issuing
        if ext_type == "notice" or lstat == "notice":
            return SourceAuthorityCategory.OFFICIAL_NOTICE, issuing
        if ext_type == "legislation":
            return SourceAuthorityCategory.PRIMARY_LEGISLATION, issuing
        if provider == "eu_immigration_portal" or lstat == "portal_html_only":
            return SourceAuthorityCategory.GOVERNMENT_PORTAL, issuing
        if provider in {"govuk_content", "national_official"} and ext_type == "guidance":
            if url and is_generic_guidance_url(url):
                return SourceAuthorityCategory.GOVERNMENT_PORTAL, issuing
            return SourceAuthorityCategory.OFFICIAL_GUIDANCE, issuing
        if host_authority_label(url):
            return SourceAuthorityCategory.OTHER_OFFICIAL_SOURCE, issuing
        return SourceAuthorityCategory.UNVERIFIED_EXTERNAL, issuing

    # internal_rag
    if url and is_generic_guidance_url(url):
        return SourceAuthorityCategory.GOVERNMENT_PORTAL, issuing
    if host and any(host == h or host.endswith(f".{h}") for h in _LEGISLATION_HOST_HINTS):
        return SourceAuthorityCategory.PRIMARY_LEGISLATION, issuing
    if lstat in {"proposed", "proposed_not_effective", "proposed_or_preparatory"}:
        return SourceAuthorityCategory.OFFICIAL_NOTICE, issuing
    if host_authority_label(url):
        return SourceAuthorityCategory.OFFICIAL_GUIDANCE, issuing
    if authority_name or authority_id is not None:
        return SourceAuthorityCategory.OTHER_OFFICIAL_SOURCE, issuing
    return SourceAuthorityCategory.UNKNOWN, issuing
