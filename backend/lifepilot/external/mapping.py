"""Deterministic jurisdiction / pathway → provider routing rules."""

from __future__ import annotations

from lifepilot.external.models import ProviderId

# GOV.UK Content API paths for known LifePilot pathways (no leading slash).
GOVUK_PATHWAY_PATHS: dict[str, str] = {
    "gb_skilled_worker": "skilled-worker-visa",
    "gb_student": "student-visa",
    "gb_graduate": "graduate-visa",
    "gb_global_talent": "global-talent",
    "gb_visitor": "standard-visitor",
}

# Extra path aliases useful for requirement-oriented lookups.
GOVUK_REQUIREMENT_HINTS: dict[str, list[str]] = {
    "eligibility": [],
    "required_documents": ["documents-you-must-provide"],
    "application_process": ["apply"],
    "financial_or_sponsorship": ["enough-money"],
    "official_provenance": [],
}

PROVIDER_JURISDICTIONS: dict[ProviderId, frozenset[str]] = {
    ProviderId.GOVUK_CONTENT: frozenset({"GB", "UK"}),
    ProviderId.CANADA_OPEN: frozenset({"CA"}),
    ProviderId.EU_OPENDATA: frozenset({"EU", "DE", "NL", "FR", "IE", "CH"}),
    ProviderId.EURLEX_CELLAR: frozenset({"EU", "DE", "NL", "FR", "IE"}),
    ProviderId.US_FEDERAL_REGISTER: frozenset({"US"}),
    ProviderId.EU_IMMIGRATION_PORTAL: frozenset({"EU", "DE", "NL", "FR", "IE"}),
}

JURISDICTION_FROM_PATHWAY_PREFIX: dict[str, str] = {
    "gb": "GB",
    "uk": "GB",
    "ca": "CA",
    "us": "US",
    "eu": "EU",
    "de": "DE",
    "nl": "NL",
    "fr": "FR",
    "ie": "IE",
    "au": "AU",
    "nz": "NZ",
    "sg": "SG",
    "ae": "AE",
    "jp": "JP",
    "ch": "CH",
}


def jurisdiction_from_pathway(pathway_id: str | None) -> str | None:
    if not pathway_id:
        return None
    prefix = pathway_id.split("_", 1)[0].lower()
    return JURISDICTION_FROM_PATHWAY_PREFIX.get(prefix)


def resolve_jurisdiction(jurisdiction: str | None, pathway_id: str | None) -> str | None:
    if jurisdiction:
        j = jurisdiction.upper()
        return "GB" if j == "UK" else j
    return jurisdiction_from_pathway(pathway_id)


def providers_for_jurisdiction(jurisdiction: str | None) -> list[ProviderId]:
    if not jurisdiction:
        # Broad discovery across all providers when jurisdiction unknown.
        return list(ProviderId)
    j = jurisdiction.upper()
    if j == "UK":
        j = "GB"
    selected = [
        pid for pid, jurisdictions in PROVIDER_JURISDICTIONS.items() if j in jurisdictions
    ]
    return selected or list(ProviderId)


def default_search_query(
    *,
    query: str,
    pathway_id: str | None,
    requirement: str | None,
    jurisdiction: str | None,
) -> str:
    parts = [p for p in (query, pathway_id, requirement, jurisdiction) if p]
    if parts:
        return " ".join(parts)
    return "immigration visa"
