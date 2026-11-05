"""EUR-Lex / CELLAR connector via public SPARQL + resource URLs.

Public SPARQL: https://publications.europa.eu/webapi/rdf/sparql
Resource: https://publications.europa.eu/resource/celex/{CELEX}

EUR-Lex SOAP search requires EU Login registration — not used here.
Results are EU legal instruments, not national eligibility rules.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import quote

from lifepilot.external.base import ExternalConnector
from lifepilot.external.models import (
    ExternalEvidence,
    ExternalQuery,
    ProviderId,
    ProviderOutcome,
    RetrievalStatus,
    SourceType,
)
from lifepilot.external.textutil import extract_passages, html_to_text

SPARQL_URL = "https://publications.europa.eu/webapi/rdf/sparql"
CELEX_RESOURCE = "https://publications.europa.eu/resource/celex"
EURLEX_HTML = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:"
ALLOWED_HOSTS = frozenset(
    {
        "publications.europa.eu",
        "eur-lex.europa.eu",
        "data.europa.eu",
    }
)

# Well-known CELEX IDs useful for immigration context (Blue Card etc.).
KNOWN_IMMIGRATION_CELEX: dict[str, str] = {
    "blue card": "32021L1883",
    "highly qualified": "32021L1883",
    "single permit": "32011L0098",
    "family reunification": "32003L0086",
    "long-term resident": "32003L0109",
    "students researchers": "32016L0801",
    "seasonal workers": "32014L0036",
}


class EurlexCellarConnector(ExternalConnector):
    provider_id: ClassVar[ProviderId] = ProviderId.EURLEX_CELLAR
    jurisdictions: ClassVar[frozenset[str]] = frozenset({"EU", "DE", "NL", "FR", "IE"})
    capabilities: ClassVar[tuple[str, ...]] = (
        "sparql_metadata",
        "celex_lookup",
        "resource_retrieval",
        "consolidated_vs_base_distinction",
    )

    def search(self, query: ExternalQuery) -> ProviderOutcome:
        celex_ids = self._resolve_celex_candidates(query)
        if not celex_ids:
            # Attempt a constrained SPARQL title search
            celex_ids = self._sparql_search_celex(query)

        if not celex_ids:
            return ProviderOutcome(
                provider=self.provider_id,
                status=RetrievalStatus.NO_RESULTS,
                error_detail=(
                    "No CELEX identifiers resolved. Full-text EUR-Lex SOAP search "
                    "requires owner EU Login registration."
                ),
                capabilities=list(self.capabilities),
            )

        items: list[ExternalEvidence] = []
        last_error: str | None = None
        last_status = RetrievalStatus.NO_RESULTS
        limit = max(1, query.max_results_per_provider)

        for celex in celex_ids[:limit]:
            meta = self._sparql_notice(celex)
            text_passages: list[str] = []
            # Prefer XHTML/HTML manifestation via content negotiation.
            html_resp = self.http.get_text(
                f"{CELEX_RESOURCE}/{quote(celex)}",
                headers={
                    "Accept": "application/xhtml+xml, text/html;q=0.9, application/pdf;q=0.1",
                    "Accept-Language": "eng,en;q=0.9",
                },
                use_cache=True,
            )
            if html_resp.ok and html_resp.text:
                text_passages = extract_passages(html_to_text(html_resp.text), max_passages=6)
            elif not html_resp.ok:
                last_status = html_resp.retrieval_status
                last_error = html_resp.error

            title = (meta or {}).get("title") or f"CELEX {celex}"
            work_type = (meta or {}).get("work_type")
            legal_status = self._classify_legal_status(celex, work_type)
            passages = text_passages or extract_passages(
                f"{title}. EU legal instrument CELEX {celex}. "
                "Directives may require national implementation and do not by "
                "themselves establish national pathway eligibility."
            )
            items.append(
                ExternalEvidence(
                    provider=self.provider_id,
                    title=title,
                    official_url=f"{EURLEX_HTML}{celex}",
                    canonical_url=f"{CELEX_RESOURCE}/{celex}",
                    document_id=celex,
                    source_type=SourceType.LEGISLATION,
                    jurisdiction="EU",
                    pathway_id=query.pathway_id,
                    requirement=query.requirement,
                    passages=passages,
                    published_at=(meta or {}).get("date"),
                    language="en",
                    license="EU reuse policy / EUR-Lex copyright notice applies",
                    legal_status=legal_status,
                    metadata={
                        "celex": celex,
                        "cellar_work": (meta or {}).get("work"),
                        "work_type": work_type,
                        "is_consolidated": celex.startswith("0"),
                        "warning": (
                            "EU legislation contextualizes requirements; it does not "
                            "replace national immigration rules."
                        ),
                        "sparql_error": None if meta else last_error,
                    },
                )
            )

        if items:
            return ProviderOutcome(
                provider=self.provider_id,
                status=RetrievalStatus.OK,
                items=items,
                capabilities=list(self.capabilities),
            )
        return ProviderOutcome(
            provider=self.provider_id,
            status=last_status,
            error_detail=last_error or "CELLAR retrieval failed",
            capabilities=list(self.capabilities),
        )

    def _resolve_celex_candidates(self, query: ExternalQuery) -> list[str]:
        found: list[str] = []
        text = " ".join(
            p for p in (query.query, query.pathway_id, query.requirement) if p
        ).lower()
        # Explicit CELEX in query
        for match in re.findall(r"\b([1230]\d{4}[A-Z]\d{3,4}(?:-\d+)?)\b", text.upper()):
            found.append(match)
        for needle, celex in KNOWN_IMMIGRATION_CELEX.items():
            if needle in text:
                found.append(celex)
        # Deduplicate preserving order
        seen: set[str] = set()
        out: list[str] = []
        for c in found:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _sparql_search_celex(self, query: ExternalQuery) -> list[str]:
        term = (query.query or "immigration").replace('"', "")[:80]
        sparql = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
SELECT DISTINCT ?celex WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  ?work cdm:work_title ?title .
  FILTER(LANG(?title) = "en" || LANG(?title) = "")
  FILTER(CONTAINS(LCASE(STR(?title)), LCASE("{term}")))
  FILTER(STRSTARTS(STR(?celex), "3") || STRSTARTS(STR(?celex), "0"))
}}
LIMIT {max(1, min(query.max_results_per_provider, 10))}
""".strip()
        resp = self.http.get_json(
            SPARQL_URL,
            params={"query": sparql, "format": "application/sparql-results+json"},
            use_cache=True,
            cache_ttl=7200.0,
        )
        if not resp.ok or not isinstance(resp.json_data, dict):
            return []
        bindings = (
            resp.json_data.get("results", {}).get("bindings", [])
            if isinstance(resp.json_data.get("results"), dict)
            else []
        )
        out: list[str] = []
        for row in bindings:
            if isinstance(row, dict) and isinstance(row.get("celex"), dict):
                val = row["celex"].get("value")
                if isinstance(val, str) and val.strip():
                    out.append(val.strip())
        return out

    def _sparql_notice(self, celex: str) -> dict[str, Any] | None:
        sparql = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?work ?title ?date ?type WHERE {{
  ?work cdm:resource_legal_id_celex "{celex}" .
  OPTIONAL {{
    ?work cdm:work_title ?title .
    FILTER(LANG(?title) = "en" || LANG(?title) = "")
  }}
  OPTIONAL {{ ?work cdm:work_date_document ?date . }}
  OPTIONAL {{ ?work a ?type . }}
}}
LIMIT 5
""".strip()
        resp = self.http.get_json(
            SPARQL_URL,
            params={"query": sparql, "format": "application/sparql-results+json"},
            use_cache=True,
            cache_ttl=7200.0,
        )
        if not resp.ok or not isinstance(resp.json_data, dict):
            return None
        bindings = (
            resp.json_data.get("results", {}).get("bindings", [])
            if isinstance(resp.json_data.get("results"), dict)
            else []
        )
        if not bindings:
            return {"title": f"CELEX {celex}"}
        row = bindings[0]
        return {
            "work": _bind(row, "work"),
            "title": _bind(row, "title") or f"CELEX {celex}",
            "date": _bind(row, "date"),
            "work_type": _bind(row, "type"),
        }

    @staticmethod
    def _classify_legal_status(celex: str, work_type: str | None) -> str:
        if celex.startswith("0"):
            return "consolidated_text"
        if celex.startswith("5"):
            return "proposed_or_preparatory"
        if work_type and "directive" in work_type.lower():
            return "directive_may_need_national_implementation"
        if work_type and "regulation" in work_type.lower():
            return "regulation"
        if celex[0:1] == "3":
            return "enacted_secondary_legislation"
        return "eu_legal_instrument"


def _bind(row: dict[str, Any], key: str) -> str | None:
    cell = row.get(key)
    if isinstance(cell, dict) and isinstance(cell.get("value"), str):
        return cell["value"]
    return None
