"""Document-to-pathway relevance (URL-scope matching) and 6G ingestion classification.

Associations are *proposed* and never treated as verified evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifepilot.crawl.storage import data_root, resolve_under_data_root

DOC_CLASSES = (
    "direct_pathway_evidence",
    "shared_cross_cutting_evidence",
    "additional_pathway_candidate",
    "information_hub",
    "out_of_current_inventory_scope",
)

# Documents fetched in Phase 6G (eight USCIS pages + one ISD Ireland page).
SIXG_NEW_DOC_ID_RANGE = (2253, 2261)

# Pathway-specific URL tokens (lowercase substrings of host+path, query stripped).
# Only true pathways are matched; hubs and broad source scope never imply relevance.
PATHWAY_URL_TOKENS: dict[str, tuple[str, ...]] = {
    "gb_skilled_worker": ("gov.uk/skilled-worker-visa",),
    "gb_student": ("gov.uk/student-visa",),
    "de_eu_blue_card": ("blauekarteeu",),
    "de_family_reunification": ("nachzug-zu-drittstaatlern",),
    "nl_student_residence": ("residence-permits/study/student-residence-permit",),
    "nl_highly_skilled_migrant": ("highly-skilled-migrant", "forms/7511", "forms/7514"),
    "nl_eu_blue_card": ("european-blue-card",),
    "nl_orientation_year": ("orientation-year",),
    "nl_start_up": ("work/start-up", "essential-start-up-personnel"),
    "nl_self_employed": ("self-employed-person",),
    "nl_family_partner": ("family-and-partner/residence-permit-for-partner",),
    "nl_permanent_residence": ("permanent-residence-permit", "forms/6009"),
    "nl_eu_long_term_resident": ("permanent-residence-permit",),
    "nl_naturalisation": ("through-naturalisation", "forms/5013", "forms/6075"),
    "sg_employment_pass": ("mom.gov.sg/passes-and-permits/employment-pass",),
    "sg_permanent_resident": (
        "permanent-residence-application",
        "pr-document-checklist",
        "completion-of-formalities-for-permanent-residence",
    ),
    "us_h1b": ("h-1b-specialty-occupations",),
    "us_l1a": ("l-1a-intracompany",),
    "us_l1b": ("l-1b-intracompany",),
    "us_o1": ("o-1-visa-individuals",),
    "us_e2": ("e-2-treaty-investors",),
    "us_e3": ("e-3-specialty-occupation",),
    "us_eb1": ("employment-based-immigration-first-preference-eb-1",),
    "us_eb2": ("employment-based-immigration-second-preference-eb-2",),
    "us_eb3": ("employment-based-immigration-third-preference-eb-3",),
    "us_eb5": ("eb-5-immigrant-investor-program",),
    "us_f1": ("students-and-exchange-visitors/students-and-employment",),
    "us_b1_b2": ("temporary-visitors-for-business/b-1-temporary-business-visitor",),
    "au_student_500": ("visa-listing/student-500",),
    "au_skills_in_demand_482": ("skills-in-demand-visa-subclass-482",),
    "au_temporary_graduate_485": ("visa-listing/temporary-graduate-485",),
    "au_citizenship": ("citizenship/become-a-citizen",),
    "ca_federal_skilled_worker": ("who-can-apply/federal-skilled-workers",),
    "ca_canadian_experience_class": ("who-can-apply/canadian-experience-class",),
    "ca_federal_skilled_trades": ("who-can-apply/federal-skilled-trades",),
    "nz_skilled_migrant": ("skilled-migrant-category-resident-visa",),
    "nz_citizenship_by_grant": ("how-to-apply-for-nz-citizenship",),
    "ie_critical_skills_employment_permit": ("critical-skills-employment-permit",),
    "ie_general_employment_permit": ("general-employment-permit",),
    "ie_intra_company_transfer": ("intra-company-transfer",),
    "ch_work_permit_non_eu": ("nicht-eu_efta-angehoerige",),
    "ae_golden_visa": ("golden-visa",),
    "ae_green_visa": ("green-visa",),
    "ae_visit_visa": ("visit-visa", "tourist-visa"),
}

# Requirement section heading cues (applied to heading text, lowercase).
REQUIREMENT_HEADING_PATTERNS: dict[str, re.Pattern[str]] = {
    "eligibility": re.compile(
        r"eligib|requirements?\b|who can|who may|who is|qualif|criteria|conditions to|"
        r"minimum requirements|can you apply|do you qualify",
        re.I,
    ),
    "required_documents": re.compile(
        r"documents?\b|evidence|checklist|what you.?ll need|supporting|proof of|"
        r"what to (?:provide|bring|submit)",
        re.I,
    ),
    "application_process": re.compile(
        r"how to apply|apply\b|application process|the process|steps|where to apply|"
        r"submit|procedure|applying",
        re.I,
    ),
    "financial_or_sponsorship": re.compile(
        r"fees?\b|costs?\b|how much|funds|income|salary|financ|sponsor|money|"
        r"remuneration|maintenance|savings|health surcharge",
        re.I,
    ),
}

MIN_SECTION_WORDS = 40


def url_key(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    path = (parts.path or "").lower().rstrip("/")
    return f"{host}{path}"


def strip_query_url(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") if parts.path != "/" else parts.path
    return f"{parts.scheme}://{(parts.hostname or '').lower()}{path}"


def match_url_to_pathways(
    url: str,
    country: str | None,
    inventory_rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Return proposed (not verified) pathway associations for a document URL."""
    key = url_key(url)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in inventory_rows:
        if row["entity_type"] != "true_pathway":
            continue
        if country and row["country"] != country:
            continue
        pid = row["pathway_id"]
        hit = ""
        for token in PATHWAY_URL_TOKENS.get(pid, ()):
            if token in key:
                hit = f"url_token:{token}"
                break
        if not hit and row.get("official_url"):
            off = url_key(row["official_url"])
            if off and (key == off or key.startswith(off + "/")):
                hit = "official_url_scope"
        if hit and pid not in seen:
            seen.add(pid)
            out.append({"pathway_id": pid, "basis": hit, "strength": "strong"})
    if len(out) > 1:
        # One page shared by several pathway entities: never use as single-pathway evidence.
        for m in out:
            m["strength"] = "shared"
    return out


# --------------------------------------------------------------------------------------
# Extraction loading / section parsing
# --------------------------------------------------------------------------------------


@dataclass
class ExtractionRecord:
    document_id: int
    version_id: int
    version_number: int
    title: str
    source_url: str
    blocks: list[dict[str, Any]]
    content_kind: str = "html_page"
    extractor_version: str = ""
    content_sha256: str = ""
    storage_path: str = ""


def block_text(block: dict[str, Any]) -> str:
    t = block.get("type")
    if t in {"heading", "paragraph"}:
        return str(block.get("text") or "")
    if t == "list":
        return " \u2022 ".join(str(i) for i in block.get("items") or [])
    if t == "table":
        parts = [str(block.get("caption") or ""), " | ".join(map(str, block.get("headers") or []))]
        for row in block.get("rows") or []:
            parts.append(" | ".join(map(str, row)))
        return " ; ".join(p for p in parts if p)
    return str(block.get("text") or "")


@dataclass
class Section:
    heading: str
    level: int
    start: int
    end: int
    text: str
    words: int

    @property
    def span(self) -> str:
        return f"blocks {self.start}-{self.end}"


def parse_sections(blocks: list[dict[str, Any]]) -> list[Section]:
    sections: list[Section] = []
    cur: dict[str, Any] | None = None
    for i, b in enumerate(blocks):
        if b.get("type") == "heading":
            if cur is not None:
                sections.append(_close(cur, i - 1))
            cur = {"heading": str(b.get("text") or ""), "level": int(b.get("level") or 2), "start": i, "texts": []}
        elif cur is not None:
            cur["texts"].append(block_text(b))
    if cur is not None:
        sections.append(_close(cur, len(blocks) - 1))
    return sections


def _close(cur: dict[str, Any], end: int) -> Section:
    body = " ".join(t for t in cur["texts"] if t)
    return Section(
        heading=cur["heading"],
        level=cur["level"],
        start=cur["start"],
        end=end,
        text=body,
        words=len(body.split()),
    )


def find_requirement_sections(
    blocks: list[dict[str, Any]], requirement: str
) -> list[Section]:
    pattern = REQUIREMENT_HEADING_PATTERNS.get(requirement)
    if pattern is None:
        return []
    out = []
    for s in parse_sections(blocks):
        if s.heading.strip().lower() in {"on this page", "page details", "contents"}:
            continue
        if pattern.search(s.heading) and s.words >= MIN_SECTION_WORDS:
            out.append(s)
    return sorted(out, key=lambda s: -s.words)


def load_current_extractions(
    session: Session,
    document_ids: list[int],
    *,
    storage_root: Path | str | None = None,
) -> dict[int, ExtractionRecord]:
    """Load current-version successful structured extractions from the data root."""
    if not document_ids:
        return {}
    root = data_root(storage_root)
    rows = session.execute(
        text(
            """
            SELECT d.id AS document_id, d.content_kind, v.id AS version_id,
                   v.version_number, e.storage_path, e.title, e.extractor_version,
                   e.content_sha256, d.canonical_url
            FROM documents d
            JOIN document_versions v ON v.document_id = d.id AND v.is_current
            JOIN document_extractions e ON e.document_version_id = v.id AND e.status = 'success'
            WHERE d.id = ANY(:ids)
            ORDER BY e.id
            """
        ),
        {"ids": list(document_ids)},
    ).mappings().all()
    out: dict[int, ExtractionRecord] = {}
    for r in rows:
        if not r["storage_path"]:
            continue
        try:
            path = resolve_under_data_root(root, r["storage_path"])
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        out[int(r["document_id"])] = ExtractionRecord(
            document_id=int(r["document_id"]),
            version_id=int(r["version_id"]),
            version_number=int(r["version_number"]),
            title=str(payload.get("title") or r["title"] or ""),
            source_url=str(payload.get("source_url") or r["canonical_url"]),
            blocks=list(payload.get("blocks") or []),
            content_kind=str(r["content_kind"]),
            extractor_version=str(r["extractor_version"] or ""),
            content_sha256=str(r["content_sha256"] or ""),
            storage_path=str(r["storage_path"]),
        )
    return out


# --------------------------------------------------------------------------------------
# PDF kind classification
# --------------------------------------------------------------------------------------

_CHECKLIST_CUES = (
    "checklist",
    "documents required",
    "required documents",
    "please enclose",
    "enclose the following",
    "supporting documents",
    "documents you",
    "following documents",
    "you must submit",
    "documents to be",
    "welke documenten",
    "bijlage",
    "meesturen",
    "bewijsstukken",
)
_FORM_CUES = (
    "signature",
    "date of birth",
    "surname",
    "first name",
    "place of birth",
    "nationality",
    "please complete",
    "handtekening",
    "geboortedatum",
    "achternaam",
    "voornamen",
    "nationaliteit",
)


_BULLET_PARAGRAPH = re.compile(r"^\s*(?:o|\u2022|\u00b7|\u25aa|\u2013|-)\s+\S")


def _item_count(block: dict[str, Any]) -> int:
    """List items, table rows, or a bullet-prefixed paragraph (PDF text bullets such as 'o ')."""
    t = block.get("type")
    if t == "list":
        return len(block.get("items") or [])
    if t == "table":
        return len(block.get("rows") or [])
    if t == "paragraph" and _BULLET_PARAGRAPH.match(str(block.get("text") or "")):
        return 1
    return 0


def classify_pdf_kind(
    blocks: list[dict[str, Any]], url: str = "", title: str = ""
) -> dict[str, Any]:
    """Distinguish an actual document list from forms/instructions/explanatory PDFs."""
    texts = [block_text(b) for b in blocks]
    full = " ".join(texts).lower()
    items = sum(_item_count(b) for b in blocks)
    checklist_hits = sum(full.count(c) for c in _CHECKLIST_CUES)
    form_hits = sum(1 for c in _FORM_CUES if c in full)
    named_checklist = "checklist" in url.lower() or "checklist" in title.lower()

    # Look for an itemised block run following a document-list cue.
    list_span = ""
    for i, t in enumerate(texts):
        tl = t.lower()
        if any(c in tl for c in _CHECKLIST_CUES):
            n = sum(_item_count(b) for b in blocks[i : i + 12])
            if n >= 4:
                list_span = f"blocks {i}-{min(i + 11, len(blocks) - 1)}"
                break
    lists_documents = bool(list_span)
    if lists_documents and (named_checklist or checklist_hits >= 2):
        kind = "document_checklist"
    elif form_hits >= 4 and not lists_documents:
        kind = "application_form"
    elif named_checklist and items >= 4:
        kind = "generic_checklist"
    else:
        kind = "instructions_or_explanatory"
    return {
        "kind": kind,
        "lists_documents": lists_documents,
        "list_span": list_span,
        "item_rows": items,
        "checklist_cue_hits": checklist_hits,
        "form_cue_hits": form_hits,
        "named_checklist": named_checklist,
    }


# --------------------------------------------------------------------------------------
# 6G USCIS / IE ingestion review (static, rationale-bearing rules)
# --------------------------------------------------------------------------------------

SPECIAL_DOC_RULES: list[dict[str, Any]] = [
    {
        "token": "temporary-workers/p-1b-",
        "classification": "additional_pathway_candidate",
        "proposed_pathway": "US P-series performer/athlete categories (P-1A/P-1B/P-2/P-3)",
        "rationale": "P-1B internationally recognized entertainment group; legitimate route not in the 6G inventory; not evidence for H-1B/L-1/O-1/EB.",
    },
    {
        "token": "temporary-workers/p-2-",
        "classification": "additional_pathway_candidate",
        "proposed_pathway": "US P-series performer/athlete categories (P-1A/P-1B/P-2/P-3)",
        "rationale": "P-2 reciprocal exchange performer; not in inventory; not evidence for specialty/intracompany/EB routes.",
    },
    {
        "token": "temporary-workers/p-3-",
        "classification": "additional_pathway_candidate",
        "proposed_pathway": "US P-series performer/athlete categories (P-1A/P-1B/P-2/P-3)",
        "rationale": "P-3 culturally unique program artist; not in inventory; not evidence for specialty/intracompany/EB routes.",
    },
    {
        "token": "temporary-workers/q-cultural-exchange",
        "classification": "additional_pathway_candidate",
        "proposed_pathway": "US Q-1 international cultural exchange",
        "rationale": "Q-1 cultural exchange route is not in the 6G inventory; does not support H-1B/L-1/O-1/EB evidence.",
    },
    {
        "token": "temporary-workers/r-1-nonimmigrant-religious-workers",
        "classification": "additional_pathway_candidate",
        "proposed_pathway": "US R-1 religious workers",
        "rationale": "R-1 religious worker route is not in the 6G inventory; does not support H-1B/L-1/O-1/EB evidence.",
    },
    {
        "token": "temporary-workers/tn-usmca-professionals",
        "classification": "additional_pathway_candidate",
        "proposed_pathway": "US TN USMCA professionals",
        "rationale": "TN (Canada/Mexico professionals) is a distinct legitimate route not in inventory; nationality-restricted; not evidence for H-1B/L-1/O-1/EB.",
    },
    {
        "token": "working-in-the-united-states/international-entrepreneur-rule",
        "classification": "additional_pathway_candidate",
        "proposed_pathway": "US International Entrepreneur Rule (parole)",
        "rationale": "International Entrepreneur Rule parole route is not in inventory; not E-2 or EB-5 evidence. Query-string variants (?acc=...&sec=...) are accordion duplicates of the base page.",
    },
    {
        "token": "stem-employment-pathways/immigrant-pathways-for-stem-employment",
        "classification": "information_hub",
        "proposed_pathway": "",
        "rationale": "Survey page of immigrant routes for STEM professionals; useful hub/navigation but cannot substitute for EB-1/EB-2/EB-3 category requirements.",
    },
    {
        "token": "irishimmigration.ie/coming-to-work-in-ireland/after-you-arrive",
        "classification": "out_of_current_inventory_scope",
        "proposed_pathway": "",
        "rationale": "Post-arrival registration/obligations for people already in Ireland; no pathway eligibility, document list or application process for an inventory pathway.",
    },
]


def classify_6g_ingested_doc(url: str) -> dict[str, Any] | None:
    key = url_key(url)
    for rule in SPECIAL_DOC_RULES:
        if rule["token"] in key:
            return rule
    return None


def near_duplicate_groups(docs: list[dict[str, Any]]) -> dict[int, int]:
    """Map document_id -> earliest document_id sharing the same URL minus query."""
    first: dict[str, int] = {}
    dup: dict[int, int] = {}
    for d in sorted(docs, key=lambda x: x["document_id"]):
        k = strip_query_url(d["canonical_url"])
        if k in first:
            dup[d["document_id"]] = first[k]
        else:
            first[k] = d["document_id"]
    return dup


def supported_requirements_from_blocks(
    blocks: list[dict[str, Any]],
    *,
    content_kind: str,
    url: str,
    title: str = "",
) -> dict[str, Any]:
    """Which critical requirements a document body plausibly supports (proposal only)."""
    supported: list[str] = []
    spans: dict[str, str] = {}
    pdf: dict[str, Any] = {}
    if content_kind == "pdf":
        pdf = classify_pdf_kind(blocks, url=url, title=title)
        if pdf["lists_documents"] and pdf["kind"] in {"document_checklist", "generic_checklist"}:
            supported.append("required_documents")
            spans["required_documents"] = pdf["list_span"]
    for req in ("eligibility", "required_documents", "application_process", "financial_or_sponsorship"):
        if req in supported:
            continue
        sections = find_requirement_sections(blocks, req)
        if sections:
            supported.append(req)
            spans[req] = sections[0].span
    return {"supported_requirements": supported, "spans": spans, "pdf": pdf}


def classify_ingested_document(
    doc: dict[str, Any],
    *,
    inventory_rows: list[dict[str, Any]],
    extraction: ExtractionRecord | None,
    intended_pathways: list[str] | None = None,
    hub_seed_urls: set[str] | None = None,
    duplicate_of: int | None = None,
) -> dict[str, Any]:
    """Classify a stored document for ``phase_6h_document_relevance.csv``."""
    url = doc["canonical_url"]
    country = doc.get("country")
    special = classify_6g_ingested_doc(url)
    matches = match_url_to_pathways(url, country, inventory_rows)
    blocks = extraction.blocks if extraction else []
    title = extraction.title if extraction else ""
    reqinfo = supported_requirements_from_blocks(
        blocks, content_kind=doc.get("content_kind", "html_page"), url=url, title=title
    ) if blocks else {"supported_requirements": [], "spans": {}, "pdf": {}}

    classification = "out_of_current_inventory_scope"
    rationale = ""
    pathways: list[str] = []
    supports = False
    proposed = ""
    if special:
        classification = special["classification"]
        rationale = special["rationale"]
        proposed = special.get("proposed_pathway", "")
        reqs: list[str] = []
    elif matches and all(m["strength"] == "strong" for m in matches):
        classification = "direct_pathway_evidence"
        pathways = [m["pathway_id"] for m in matches]
        supports = True
        reqs = reqinfo["supported_requirements"]
        rationale = (
            f"URL scope matches pathway ({matches[0]['basis']}); "
            f"proposed association, not verified evidence."
        )
        pdf = reqinfo.get("pdf") or {}
        if pdf:
            rationale += (
                f" PDF kind={pdf.get('kind')}; lists_documents={pdf.get('lists_documents')}."
            )
            if pdf.get("kind") in {"application_form", "instructions_or_explanatory"} and not pdf.get(
                "lists_documents"
            ):
                reqs = [r for r in reqs if r != "required_documents"]
                rationale += " Does not itself establish an itemised document list."
    elif matches:
        classification = "shared_cross_cutting_evidence"
        pathways = [m["pathway_id"] for m in matches]
        rationale = (
            "Page URL matches several pathway entities; shared evidence cannot be attributed "
            "to one pathway without separate review."
        )
        reqs = []
    else:
        reqs = []
        intended = intended_pathways or []
        if hub_seed_urls and strip_query_url(url) in hub_seed_urls:
            classification = "information_hub"
            pathways = list(intended)
            rationale = (
                "Source seed/hub page; introductory guidance that does not by itself establish "
                "complete eligibility, documents or process for specific pathways."
            )
        elif intended:
            classification = "information_hub"
            pathways = list(intended)
            rationale = "Under an approved narrow source but no pathway-specific URL scope match."
        else:
            rationale = "No pathway URL scope match and no intended pathway."
    return {
        "document_id": doc["document_id"],
        "version_id": doc.get("version_id") or (extraction.version_id if extraction else ""),
        "canonical_url": url,
        "source_id": doc.get("source_id"),
        "country": country,
        "content_kind": doc.get("content_kind"),
        "language": doc.get("language") or doc.get("document_language") or "",
        "classification": classification,
        "intended_or_matched_pathways": ",".join(pathways),
        "supports_intended_pathway": supports,
        "supported_requirements": ",".join(reqs),
        "requirement_spans": json.dumps(reqinfo["spans"], ensure_ascii=False) if reqinfo["spans"] else "",
        "pdf_kind": (reqinfo.get("pdf") or {}).get("kind", ""),
        "pdf_lists_documents": (reqinfo.get("pdf") or {}).get("lists_documents", ""),
        "proposed_additional_pathway": proposed,
        "near_duplicate_of_document_id": duplicate_of or "",
        "association_status": "proposed_not_verified",
        "rationale": rationale,
        "extraction_status": doc.get("extraction_status") or ("success" if extraction else ""),
    }


def load_documents_by_ids(session: Session, ids: list[int]) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT d.id AS document_id, d.canonical_url, d.primary_source_id AS source_id,
                   d.content_kind, d.language AS document_language,
                   v.id AS version_id, v.language, v.retrieved_at,
                   COALESCE(j.country_code, j.code) AS country,
                   (SELECT e.status FROM document_extractions e
                     WHERE e.document_version_id = v.id ORDER BY e.id DESC LIMIT 1)
                     AS extraction_status
            FROM documents d
            JOIN document_versions v ON v.document_id = d.id AND v.is_current
            LEFT JOIN sources s ON s.id = d.primary_source_id
            LEFT JOIN jurisdictions j ON j.id = s.jurisdiction_id
            WHERE d.id = ANY(:ids)
            ORDER BY d.id
            """
        ),
        {"ids": ids},
    ).mappings().all()
    return [dict(r) for r in rows]


@dataclass
class AssociationProposal:
    document_id: int
    pathway_id: str
    strength: str
    basis: str
    status: str = "proposed_not_verified"
    extra: dict[str, Any] = field(default_factory=dict)


def propose_associations(
    docs: list[dict[str, Any]], inventory_rows: list[dict[str, Any]]
) -> list[AssociationProposal]:
    out: list[AssociationProposal] = []
    for d in docs:
        for m in match_url_to_pathways(d["canonical_url"], d.get("country"), inventory_rows):
            out.append(
                AssociationProposal(
                    document_id=d["document_id"],
                    pathway_id=m["pathway_id"],
                    strength=m["strength"],
                    basis=m["basis"],
                )
            )
    return out
