"""Parent categories, requirement field registry, and config loaders."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

AUDIT_COUNTRIES: tuple[str, ...] = (
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

PARENT_CATEGORIES: tuple[str, ...] = (
    "work",
    "study",
    "family",
    "permanent_residence",
    "citizenship",
    "eu_residence",
)

# Country-relevant parent slots (others stay inventoried as N/A notes only via EU).
COUNTRY_PARENT_SLOTS: dict[str, tuple[str, ...]] = {
    "GB": ("work", "study", "family", "permanent_residence", "citizenship"),
    "DE": ("work", "study", "family", "permanent_residence", "citizenship"),
    "NL": ("work", "study", "family", "permanent_residence", "citizenship"),
    "SG": ("work", "study", "family", "permanent_residence", "citizenship"),
    "US": ("work", "study", "family", "permanent_residence", "citizenship"),
    "AU": ("work", "study", "family", "permanent_residence", "citizenship"),
    "CA": ("work", "study", "family", "permanent_residence", "citizenship"),
    "NZ": ("work", "study", "family", "permanent_residence", "citizenship"),
    "IE": ("work", "study", "family", "permanent_residence", "citizenship"),
    "CH": ("work", "study", "family", "permanent_residence", "citizenship"),
    "AE": ("work", "study", "family", "permanent_residence", "citizenship"),
    "JP": ("work", "study", "family", "permanent_residence", "citizenship"),
    "FR": ("work", "study", "family", "permanent_residence", "citizenship"),
    "EU": ("eu_residence", "work", "family"),
}

REQUIREMENT_FIELDS: tuple[str, ...] = (
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "fees_costs",
    "processing_times",
    "conditions_duration",
    "language_requirements",
    "health_character",
    "dependents",
    "official_provenance",
)

# Release-readiness blockers when applicable.
CRITICAL_FIELDS: frozenset[str] = frozenset(
    {
        "eligibility",
        "required_documents",
        "application_process",
        "financial_or_sponsorship",
        "official_provenance",
    }
)

# Thin-hub heuristics (navigation pages cannot be COMPLETE).
THIN_MIN_TEXT_CHARS = 800
THIN_MIN_BLOCKS = 8
THIN_MIN_PARAGRAPH_CHARS = 400


@dataclass(frozen=True)
class PathwaySeed:
    pathway_id: str
    country: str
    parent_category: str
    name: str
    source_ids: tuple[int, ...]
    match_titles: tuple[str, ...]
    match_headings: tuple[str, ...]
    match_urls: tuple[str, ...]
    financial_applicable: bool = True
    notes: tuple[str, ...] = ()
    provenance: str = ""


@dataclass
class RequirementSignal:
    field: str
    heading_patterns: list[str] = field(default_factory=list)
    keyword_patterns: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=lambda: ["en"])


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def config_dir() -> Path:
    return _repo_root() / "config" / "coverage"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_pathway_seeds(
    path: str | None = None,
) -> tuple[PathwaySeed, ...]:
    cfg_path = Path(path) if path else config_dir() / "pathway_seeds.json"
    raw = load_json(cfg_path)
    seeds: list[PathwaySeed] = []
    for item in raw.get("pathways", []):
        prov = item.get("provenance") or {}
        source_ids = tuple(int(x) for x in (prov.get("source_ids") or item.get("source_ids") or []))
        seeds.append(
            PathwaySeed(
                pathway_id=str(item["pathway_id"]),
                country=str(item["country"]).upper(),
                parent_category=str(item["parent_category"]),
                name=str(item["name"]),
                source_ids=source_ids,
                match_titles=tuple(str(x).lower() for x in (item.get("match_titles") or [])),
                match_headings=tuple(
                    str(x).lower() for x in (item.get("match_headings") or [])
                ),
                match_urls=tuple(str(x).lower() for x in (item.get("match_urls") or [])),
                financial_applicable=bool(item.get("financial_applicable", True)),
                notes=tuple(str(x) for x in (item.get("notes") or [])),
                provenance=str(prov.get("label") or item.get("provenance_label") or ""),
            )
        )
    return tuple(seeds)


@lru_cache(maxsize=1)
def load_requirement_signals(
    path: str | None = None,
) -> dict[str, RequirementSignal]:
    cfg_path = Path(path) if path else config_dir() / "requirement_signals.json"
    raw = load_json(cfg_path)
    out: dict[str, RequirementSignal] = {}
    for field_key, spec in (raw.get("signals") or {}).items():
        out[str(field_key)] = RequirementSignal(
            field=str(field_key),
            heading_patterns=[str(x).lower() for x in (spec.get("heading_patterns") or [])],
            keyword_patterns=[str(x).lower() for x in (spec.get("keyword_patterns") or [])],
            languages=[str(x).lower() for x in (spec.get("languages") or ["en"])],
        )
    return out


def is_critical_field(field: str, *, financial_applicable: bool) -> bool:
    if field not in CRITICAL_FIELDS:
        return False
    if field == "financial_or_sponsorship" and not financial_applicable:
        return False
    return True


def field_applicable(field: str, *, financial_applicable: bool) -> bool:
    if field == "financial_or_sponsorship" and not financial_applicable:
        return False
    return True
