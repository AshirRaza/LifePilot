"""Owner-authorized exact URL candidates for Phase 6H (and deduped pending queue)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lifepilot.registry.urls import InvalidRegistryUrlError, normalize_url

PLAN_EXACT_ALLOWLIST = "exact_allowlist"
PLAN_NEW_SOURCE = "new_source"
PLAN_EXISTING_SCOPE = "existing_scope"


@dataclass(frozen=True)
class NewSourceSpec:
    """Narrow new Source registration (never whole-domain)."""

    name: str
    authority_id: int
    jurisdiction_id: int
    seed_url: str
    default_language: str = "en"
    notes: str = ""


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    jurisdiction: str
    pathway_ids: tuple[str, ...]
    authorized_url: str
    plan: str
    expected_hosts: tuple[str, ...]
    source_id: int | None = None
    new_source: NewSourceSpec | None = None
    # Label when the page is not accessible (owner-specified wording).
    not_accessible_label: str = "rejected_broken"
    # Owner-acknowledged relevance caveat (e.g. PDF whose body is checked post-ingest).
    relevance_waiver: str = ""
    # Titles that indicate a generic hub rather than the intended pathway page.
    hub_title_markers: tuple[str, ...] = ()
    intended_scope: str = ""

    @property
    def scope_key(self) -> str:
        return self.intended_scope or self.pathway_ids[0]


def authorized_candidates() -> list[Candidate]:
    """The twelve exact URLs authorized for Phase 6H review."""
    return [
        Candidate(
            candidate_id="IE-critical-skills-checklist",
            jurisdiction="IE",
            pathway_ids=("ie_critical_skills_employment_permit",),
            authorized_url=(
                "https://enterprise.gov.ie/en/publications/publication-files/"
                "critical-skills-employment-permits-checklist.pdf"
            ),
            plan=PLAN_EXACT_ALLOWLIST,
            expected_hosts=("enterprise.gov.ie", "www.enterprise.gov.ie"),
            source_id=9090,
        ),
        Candidate(
            candidate_id="IE-general-employment-permit-checklist",
            jurisdiction="IE",
            pathway_ids=("ie_general_employment_permit",),
            authorized_url=(
                "https://enterprise.gov.ie/en/Publications/Publication-files/"
                "General-Employment-Permits-Checklist.pdf"
            ),
            plan=PLAN_EXACT_ALLOWLIST,
            expected_hosts=("enterprise.gov.ie", "www.enterprise.gov.ie"),
            source_id=9090,
        ),
        Candidate(
            candidate_id="IE-employment-permits-checklists-index",
            jurisdiction="IE",
            pathway_ids=("ie_employment_permits_hub",),
            authorized_url=(
                "https://enterprise.gov.ie/en/Publications/Employment-Permits-Checklists.html"
            ),
            plan=PLAN_EXACT_ALLOWLIST,
            expected_hosts=("enterprise.gov.ie", "www.enterprise.gov.ie"),
            source_id=9090,
        ),
        Candidate(
            candidate_id="NL-ind-form-6009",
            jurisdiction="NL",
            pathway_ids=("nl_permanent_residence",),
            authorized_url="https://ind.nl/en/forms/6009.pdf",
            plan=PLAN_EXACT_ALLOWLIST,
            expected_hosts=("ind.nl", "www.ind.nl"),
            source_id=11039,
            relevance_waiver=(
                "Owner-named exact IND form linked from approved permanent-residence page; "
                "PDF body relevance is classified after extraction (document_relevance)."
            ),
        ),
        Candidate(
            candidate_id="AE-uae-portal-visa-emirates-id",
            jurisdiction="AE",
            pathway_ids=("ae_work_residence",),
            authorized_url="https://u.ae/en/information-and-services/visa-and-emirates-id",
            plan=PLAN_NEW_SOURCE,
            expected_hosts=("u.ae", "www.u.ae"),
            new_source=NewSourceSpec(
                name="UAE Official Portal \u2014 Visa and Emirates ID",
                authority_id=8171,
                jurisdiction_id=6941,
                seed_url="https://u.ae/en/information-and-services/visa-and-emirates-id",
                notes=(
                    "Phase 6H owner-authorized narrow seed. Federal portal hub; not "
                    "emirate-specific procedures and not complete work/student/family coverage."
                ),
            ),
        ),
        Candidate(
            candidate_id="JP-isa-engineer-specialist",
            jurisdiction="JP",
            pathway_ids=("jp_engineer_specialist",),
            authorized_url="https://www.moj.go.jp/isa/applications/status/engineer.html",
            plan=PLAN_NEW_SOURCE,
            expected_hosts=("www.moj.go.jp", "moj.go.jp"),
            new_source=NewSourceSpec(
                name="ISA \u2014 Engineer / Specialist in Humanities / International Services",
                authority_id=6669,
                jurisdiction_id=6939,
                seed_url="https://www.moj.go.jp/isa/applications/status/engineer.html",
                default_language="ja",
            ),
            not_accessible_label="rejected_not_found",
        ),
        Candidate(
            candidate_id="FR-service-public-n19804",
            jurisdiction="FR",
            pathway_ids=("fr_passeport_talent",),
            authorized_url="https://www.service-public.fr/particuliers/vosdroits/N19804",
            plan=PLAN_NEW_SOURCE,
            expected_hosts=(
                "www.service-public.fr",
                "service-public.fr",
                "www.service-public.gouv.fr",
                "service-public.gouv.fr",
            ),
            new_source=NewSourceSpec(
                name="Service-Public \u2014 Passeport talent (N19804)",
                authority_id=6673,
                jurisdiction_id=6942,
                seed_url="https://www.service-public.fr/particuliers/vosdroits/N19804",
                default_language="fr",
            ),
            hub_title_markers=("\u00e9tranger - europe",),
        ),
        Candidate(
            candidate_id="GB-student-visa",
            jurisdiction="GB",
            pathway_ids=("gb_student",),
            authorized_url="https://www.gov.uk/student-visa",
            plan=PLAN_NEW_SOURCE,
            expected_hosts=("www.gov.uk", "gov.uk"),
            new_source=NewSourceSpec(
                name="UK Student visa guidance",
                authority_id=68,
                jurisdiction_id=100,
                seed_url="https://www.gov.uk/student-visa",
                notes="Phase 6H: new narrow source; source 86 (Skilled Worker) is not expanded.",
            ),
        ),
        Candidate(
            candidate_id="DE-bamf-eu-blue-card",
            jurisdiction="DE",
            pathway_ids=("de_eu_blue_card",),
            authorized_url=(
                "https://www.bamf.de/EN/Themen/MigrationAufenthalt/ZuwandererDrittstaaten/"
                "Migrathek/BlaueKarteEU/blauekarteeu-node.html"
            ),
            plan=PLAN_NEW_SOURCE,
            expected_hosts=("www.bamf.de", "bamf.de"),
            new_source=NewSourceSpec(
                name="BAMF \u2014 EU Blue Card",
                authority_id=8168,
                jurisdiction_id=101,
                seed_url=(
                    "https://www.bamf.de/EN/Themen/MigrationAufenthalt/ZuwandererDrittstaaten/"
                    "Migrathek/BlaueKarteEU/blauekarteeu-node.html"
                ),
                notes="Phase 6H: exact Blue Card node only; not a general BAMF crawl.",
            ),
        ),
        Candidate(
            candidate_id="US-uscis-students-and-employment",
            jurisdiction="US",
            pathway_ids=("us_f1",),
            authorized_url=(
                "https://www.uscis.gov/working-in-the-united-states/"
                "students-and-exchange-visitors/students-and-employment"
            ),
            plan=PLAN_EXISTING_SCOPE,
            expected_hosts=("www.uscis.gov", "uscis.gov"),
            source_id=9080,
        ),
        Candidate(
            candidate_id="AU-temporary-graduate-485",
            jurisdiction="AU",
            pathway_ids=("au_temporary_graduate_485",),
            authorized_url=(
                "https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/"
                "temporary-graduate-485"
            ),
            plan=PLAN_NEW_SOURCE,
            expected_hosts=("immi.homeaffairs.gov.au",),
            new_source=NewSourceSpec(
                name="Home Affairs \u2014 Temporary Graduate visa (subclass 485)",
                authority_id=6668,
                jurisdiction_id=6938,
                seed_url=(
                    "https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/"
                    "temporary-graduate-485"
                ),
            ),
        ),
        Candidate(
            candidate_id="NZ-check-if-you-can-apply",
            jurisdiction="NZ",
            pathway_ids=("nz_citizenship_by_grant",),
            authorized_url=(
                "https://www.govt.nz/browse/passports-citizenship-and-identity/nz-citizenship/"
                "how-to-apply-for-nz-citizenship/check-if-you-can-apply-for-nz-citizenship/"
            ),
            plan=PLAN_EXISTING_SCOPE,
            expected_hosts=("www.govt.nz", "govt.nz"),
            source_id=11032,
            not_accessible_label="rejected_broken",
        ),
    ]


def safe_normalize(url: str) -> str:
    try:
        return normalize_url(url)
    except InvalidRegistryUrlError:
        return url.strip().lower()


@dataclass
class PendingRow:
    jurisdiction: str
    pathway: str
    url: str
    status: str
    reason: str
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str]:
        norm = safe_normalize(self.url) if self.url else f"nourl:{self.pathway}:{self.status}"
        return norm, self.pathway

    def to_row(self) -> dict[str, Any]:
        return {
            "jurisdiction": self.jurisdiction,
            "pathway": self.pathway,
            "url": self.url,
            "normalized_url": safe_normalize(self.url) if self.url else "",
            "approval_status": self.status,
            "reason": self.reason,
            "origin": self.source,
            **self.extra,
        }


def dedupe_pending_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe by normalized URL + intended pathway scope; merge origins.

    Fixes the 6G defect where one URL appeared as both a specific proposal and an
    inventory-generated proposal (e.g. GB Student).
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        url = str(row.get("url") or "")
        pathway = str(row.get("pathway") or "")
        norm = safe_normalize(url) if url else f"nourl:{pathway}:{row.get('approval_status')}"
        key = (norm, pathway)
        if key not in merged:
            out = dict(row)
            out["normalized_url"] = safe_normalize(url) if url else ""
            out["merged_origins"] = [str(row.get("origin") or "")]
            merged[key] = out
            continue
        cur = merged[key]
        origin = str(row.get("origin") or "")
        if origin and origin not in cur["merged_origins"]:
            cur["merged_origins"].append(origin)
        # Prefer the more specific (non inventory-generated) status/reason.
        if str(cur.get("approval_status", "")).startswith("pending_new_source_or_allowlist"):
            cur["approval_status"] = row.get("approval_status", cur["approval_status"])
            cur["reason"] = row.get("reason", cur.get("reason", ""))
    out_rows = []
    for item in merged.values():
        item["merged_origins"] = "; ".join(o for o in item["merged_origins"] if o)
        out_rows.append(item)
    return out_rows


def build_pending_approvals(
    *,
    decisions: list[dict[str, Any]],
    inventory: list[dict[str, Any]],
    candidate_sources: list[dict[str, Any]],
    prior_queue_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Queue items still needing separate human approval (no substitute fetching)."""
    rows: list[dict[str, Any]] = []
    by_id = {d["candidate_id"]: d for d in decisions}

    jp = by_id.get("JP-isa-engineer-specialist")
    if jp and str(jp.get("decision_code", "")).startswith("rejected"):
        rows.append(
            {
                "jurisdiction": "JP",
                "pathway": "jp_engineer_specialist",
                "url": "",
                "approval_status": "pending_replacement_research",
                "reason": (
                    "Authorized ISA engineer.html returned HTTP 404 (rejected_not_found). "
                    "Research the current ISA status page for Engineer/Specialist in "
                    "Humanities/International Services; no substitute was fetched."
                ),
                "origin": "phase_6h_decision",
                "priority": 1,
            }
        )
    fr = by_id.get("FR-service-public-n19804")
    if fr and str(fr.get("decision_code", "")).startswith("rejected"):
        final = str(fr.get("final_url") or "")
        if final:
            rows.append(
                {
                    "jurisdiction": "FR",
                    "pathway": "fr_foreigners_hub",
                    "url": final,
                    "approval_status": "pending_separate_approval_hub_only",
                    "reason": (
                        "Redirects to service-public.gouv.fr N19804; title is a generic "
                        "'\u00c9tranger - Europe' hub, not passeport talent. Would be hub-scope "
                        "only if separately approved."
                    ),
                    "origin": "phase_6h_decision",
                    "priority": 2,
                }
            )
        rows.append(
            {
                "jurisdiction": "FR",
                "pathway": "fr_passeport_talent",
                "url": "",
                "approval_status": "pending_replacement_research",
                "reason": (
                    "Need a passeport-talent-specific official page (Service-Public or "
                    "Interior) for separate approval; none was invented or fetched."
                ),
                "origin": "phase_6h_decision",
                "priority": 1,
            }
        )
    nz = by_id.get("NZ-check-if-you-can-apply")
    if nz and str(nz.get("decision_code", "")).startswith("rejected"):
        rows.append(
            {
                "jurisdiction": "NZ",
                "pathway": "nz_citizenship_by_grant",
                "url": "",
                "approval_status": "pending_replacement_research",
                "reason": (
                    "Authorized check-if-you-can-apply URL returned HTTP 404 "
                    "(rejected_broken); parent how-to-apply source remains approved. "
                    "Research current grant-eligibility page."
                ),
                "origin": "phase_6h_decision",
                "priority": 2,
            }
        )
    ae = by_id.get("AE-uae-portal-visa-emirates-id")
    if ae and str(ae.get("decision_code", "")).startswith("approved"):
        rows.append(
            {
                "jurisdiction": "AE",
                "pathway": "ae_work_residence",
                "url": "",
                "approval_status": "pending_pathway_specific_research",
                "reason": (
                    "Approved UAE portal seed is a Visa and Emirates ID hub; employment, "
                    "student, family, Golden/Green visa pages need separate exact-URL approval. "
                    "Emirate-specific procedures are not covered."
                ),
                "origin": "phase_6h_decision",
                "priority": 1,
            }
        )

    for src in candidate_sources:
        rows.append(
            {
                "jurisdiction": src.get("country_code") or "",
                "pathway": f"source:{src['id']}",
                "url": src.get("seed_url") or "",
                "approval_status": "candidate_source_awaiting_review",
                "reason": f"Registered source {src['id']} ({src.get('name')}) has review_status=candidate.",
                "origin": "sources_table",
                "priority": 2,
                "source_id": src["id"],
            }
        )

    decided_urls = {safe_normalize(d["authorized_url"]) for d in decisions}
    decided_urls |= {safe_normalize(d["final_url"]) for d in decisions if d.get("final_url")}
    resolved_pathways = {
        p
        for d in decisions
        if str(d.get("decision_code", "")).startswith("approved")
        for p in str(d.get("pathway_ids", "")).split(",")
        if p
    }
    for item in inventory:
        if item["entity_type"] != "true_pathway":
            continue
        if item.get("source_ids") or item["pathway_id"] in resolved_pathways:
            continue
        url = item.get("official_url") or ""
        if url and safe_normalize(url) in decided_urls:
            continue
        rows.append(
            {
                "jurisdiction": item["country"],
                "pathway": item["pathway_id"],
                "url": url,
                "approval_status": (
                    "pending_new_source_or_allowlist" if url else "needs_url_research"
                ),
                "reason": "Inventory true pathway without an approved linked source.",
                "origin": "inventory_gap",
                "priority": 3,
            }
        )

    for row in prior_queue_rows:
        status = str(row.get("approval_status", ""))
        if not status.startswith("pending"):
            continue
        url = str(row.get("url") or "")
        if url and safe_normalize(url) in decided_urls:
            continue
        rows.append(
            {
                "jurisdiction": row.get("jurisdiction", ""),
                "pathway": row.get("pathway", ""),
                "url": url,
                "approval_status": status,
                "reason": row.get("discovery_origin", ""),
                "origin": "phase_6g_approval_queue",
                "priority": row.get("priority", 3),
            }
        )

    return dedupe_pending_rows(rows)
