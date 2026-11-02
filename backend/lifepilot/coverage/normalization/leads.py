"""Validate gap-register candidate URLs without fetching."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

from lifepilot.coverage.normalization.normalize import load_normalization_config

LeadClass = str  # pathway_specific | general_guidance | unrelated_generic | unverified_lead


@dataclass
class LeadAssessment:
    gap_id: str
    pathway_id: str
    country: str
    url: str
    lead_class: LeadClass
    reason: str
    reused_generic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _classify_url(
    url: str,
    pathway_id: str,
    normalized_id: str,
    *,
    config: dict[str, Any] | None = None,
) -> tuple[LeadClass, str]:
    cfg = config or load_normalization_config()
    rules = cfg.get("lead_url_rules") or {}
    generic = [p.lower() for p in (rules.get("generic_patterns") or [])]
    hints_map = rules.get("pathway_url_hints") or {}
    # Try both original and normalized ids for hints
    hints = [h.lower() for h in (hints_map.get(normalized_id) or hints_map.get(pathway_id) or [])]

    low = url.lower()
    path = urlparse(url).path.lower()

    for g in generic:
        if g in low:
            return "unrelated_generic", f"matches generic pattern '{g}'"

    if hints and any(h in low or h in path for h in hints):
        return "pathway_specific", "URL path matches pathway-specific hint"

    # Official host but generic path
    host = urlparse(url).netloc.lower()
    official_hosts = (
        "homeaffairs.gov.au",
        "immi.homeaffairs.gov.au",
        "gov.uk",
        "uscis.gov",
        "ind.nl",
        "canada.ca",
        "immigration.govt.nz",
        "mom.gov.sg",
        "bamf.de",
    )
    if any(h in host for h in official_hosts):
        if any(
            x in low
            for x in ("overview", "faq", "help-support", "contact", "login")
        ):
            return "general_guidance", "official host but general/help path"
        return "general_guidance", "official host without pathway-specific hint"

    if url.startswith("http"):
        return "unverified_lead", "URL present but not validated as pathway-specific"

    return "unverified_lead", "empty or non-http URL"


def assess_gap_leads(
    gap_rows: list[dict[str, str]],
    *,
    original_to_normalized: dict[str, str],
    config: dict[str, Any] | None = None,
) -> list[LeadAssessment]:
    cfg = config or load_normalization_config()
    seen_url_pathways: dict[str, set[str]] = {}
    out: list[LeadAssessment] = []

    for row in gap_rows:
        url = (row.get("potential_lead_url") or "").strip()
        if not url:
            continue
        pid = row.get("pathway_id") or ""
        nid = original_to_normalized.get(pid, pid)
        lead_class, reason = _classify_url(url, pid, nid, config=cfg)
        reused = False
        pathways_for_url = seen_url_pathways.setdefault(url, set())
        if pathways_for_url and pid not in pathways_for_url:
            reused = True
            if lead_class != "pathway_specific":
                lead_class = "unrelated_generic"
                reason = (
                    f"same URL reused across pathways {sorted(pathways_for_url | {pid})}; "
                    + reason
                )
        pathways_for_url.add(pid)
        out.append(
            LeadAssessment(
                gap_id=row.get("gap_id") or "",
                pathway_id=pid,
                country=row.get("country") or "",
                url=url,
                lead_class=lead_class,
                reason=reason,
                reused_generic=reused,
            )
        )
    return out


def invalid_or_generic_count(assessments: list[LeadAssessment]) -> int:
    return sum(
        1
        for a in assessments
        if a.lead_class in {"unrelated_generic", "unverified_lead"} or a.reused_generic
    )
