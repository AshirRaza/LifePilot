"""US Federal Register API v1 connector.

Docs: https://www.federalregister.gov/developers/documentation/api/v1
No API key required.
"""

from __future__ import annotations

from typing import Any, ClassVar

from lifepilot.external.base import ExternalConnector
from lifepilot.external.mapping import default_search_query
from lifepilot.external.models import (
    ExternalEvidence,
    ExternalQuery,
    ProviderId,
    ProviderOutcome,
    RetrievalStatus,
    SourceType,
)
from lifepilot.external.textutil import extract_passages

SEARCH_URL = "https://www.federalregister.gov/api/v1/documents.json"
ALLOWED_HOSTS = frozenset({"www.federalregister.gov"})

# Agency slugs commonly relevant to immigration.
DEFAULT_AGENCIES = ("homeland-security-department", "u-s-citizenship-and-immigration-services")


class UsFederalRegisterConnector(ExternalConnector):
    provider_id: ClassVar[ProviderId] = ProviderId.US_FEDERAL_REGISTER
    jurisdictions: ClassVar[frozenset[str]] = frozenset({"US"})
    capabilities: ClassVar[tuple[str, ...]] = (
        "document_search",
        "agency_filter",
        "date_filter",
        "pagination",
        "document_types",
    )

    def search(self, query: ExternalQuery) -> ProviderOutcome:
        term = default_search_query(
            query=query.query or "immigration",
            pathway_id=query.pathway_id,
            requirement=query.requirement,
            jurisdiction=None,
        )
        per_page = max(1, min(query.max_results_per_provider, 100))
        params: list[tuple[str, str]] = [
            ("per_page", str(per_page)),
            ("page", "1"),
            ("order", "relevance"),
            ("conditions[term]", term),
        ]
        for agency in DEFAULT_AGENCIES:
            params.append(("conditions[agencies][]", agency))
        if query.published_after:
            params.append(("conditions[publication_date][gte]", query.published_after))
        if query.published_before:
            params.append(("conditions[publication_date][lte]", query.published_before))

        # Use doseq URL encoding so repeated conditions[agencies][] are preserved.
        resp = self._search_with_raw_params(params)
        if not resp.ok:
            return ProviderOutcome(
                provider=self.provider_id,
                status=resp.retrieval_status,
                error_detail=resp.error,
                capabilities=list(self.capabilities),
            )
        data = resp.json_data if isinstance(resp.json_data, dict) else {}
        results = data.get("results") if isinstance(data.get("results"), list) else []
        if not results:
            return ProviderOutcome(
                provider=self.provider_id,
                status=RetrievalStatus.NO_RESULTS,
                error_detail="No matching Federal Register documents",
                capabilities=list(self.capabilities),
            )

        items = [
            self._doc_to_evidence(doc, query)
            for doc in results[:per_page]
            if isinstance(doc, dict)
        ]
        return ProviderOutcome(
            provider=self.provider_id,
            status=RetrievalStatus.OK,
            items=items,
            capabilities=list(self.capabilities),
        )

    def _search_with_raw_params(
        self, params: list[tuple[str, str]]
    ):
        from urllib.parse import urlencode

        url = f"{SEARCH_URL}?{urlencode(params, doseq=True)}"
        return self.http.get_json(url)

    def _doc_to_evidence(
        self, doc: dict[str, Any], query: ExternalQuery
    ) -> ExternalEvidence:
        doc_type = str(doc.get("type") or "")
        source_type = _map_type(doc_type)
        abstract = doc.get("abstract") or doc.get("excerpts") or ""
        if isinstance(abstract, list):
            abstract = " ".join(str(a) for a in abstract)
        title = str(doc.get("title") or doc.get("document_number") or "Federal Register")
        html_url = str(doc.get("html_url") or "")
        doc_number = str(doc.get("document_number") or "")
        agencies = []
        for a in doc.get("agencies") or []:
            if isinstance(a, dict):
                agencies.append(a.get("name") or a.get("raw_name") or a.get("slug"))
        legal_status = _legal_status(doc_type, doc)
        passages = extract_passages(
            f"{title}\n{abstract}\n"
            f"Document type: {doc_type}. "
            "Federal Register entries are not a complete eligibility assessment."
        )
        return ExternalEvidence(
            provider=self.provider_id,
            title=title,
            official_url=html_url,
            canonical_url=html_url,
            document_id=doc_number,
            source_type=source_type,
            jurisdiction="US",
            pathway_id=query.pathway_id,
            requirement=query.requirement,
            passages=passages,
            published_at=_as_str(doc.get("publication_date")),
            effective_at=_as_str(doc.get("effective_on") or doc.get("dates")),
            language="en",
            license="U.S. government work / Federal Register reuse terms",
            legal_status=legal_status,
            metadata={
                "type": doc_type,
                "agencies": agencies,
                "pdf_url": doc.get("pdf_url"),
                "warning": (
                    "Proposed rules and notices are not the same as currently "
                    "effective eligibility requirements."
                ),
            },
        )


def _map_type(doc_type: str) -> SourceType:
    t = doc_type.lower()
    if "proposed" in t:
        return SourceType.PROPOSED_RULE
    if t == "rule" or "final" in t:
        return SourceType.FINAL_RULE
    if "notice" in t:
        return SourceType.NOTICE
    return SourceType.NOTICE


def _legal_status(doc_type: str, doc: dict[str, Any]) -> str:
    t = doc_type.lower()
    if "proposed" in t:
        return "proposed_not_effective"
    if t == "rule" or "final" in t:
        if doc.get("effective_on"):
            return "final_rule_check_effective_date"
        return "final_rule"
    if "notice" in t:
        return "notice"
    return "federal_register_document"


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
