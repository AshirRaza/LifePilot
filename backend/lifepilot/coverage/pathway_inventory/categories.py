"""Phase 6G inventory categories and jurisdictions."""

from __future__ import annotations

JURISDICTIONS: tuple[str, ...] = (
    "GB",
    "DE",
    "NL",
    "SG",
    "US",
    "AU",
    "CA",
    "NZ",
    "IE",
    "CH",
    "AE",
    "JP",
    "FR",
    "EU",
)

CATEGORIES: tuple[str, ...] = (
    "short_term",
    "education",
    "employment",
    "business",
    "family",
    "long_term_residence",
    "citizenship",
    "cross_cutting",
)

ENTITY_TYPES: tuple[str, ...] = (
    "true_pathway",
    "information_hub",
    "cross_cutting_guidance",
    "unknown_candidate",
)

# Map legacy parent_category → 6G category
LEGACY_CATEGORY_MAP: dict[str, str] = {
    "work": "employment",
    "study": "education",
    "family": "family",
    "permanent_residence": "long_term_residence",
    "citizenship": "citizenship",
    "eu_residence": "long_term_residence",
}
