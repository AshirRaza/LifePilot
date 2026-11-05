"""EU Open Data Portal (data.europa.eu) Search API connector.

Docs: https://dataeuropa.gitlab.io/data-provider-manual/api-documentation/
Search: GET https://data.europa.eu/api/hub/search/search
"""

from __future__ import annotations

import json
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

SEARCH_URL = "https://data.europa.eu/api/hub/search/search"
ALLOWED_HOSTS = frozenset({"data.europa.eu"})


class EuOpenDataConnector(ExternalConnector):
    provider_id: ClassVar[ProviderId] = ProviderId.EU_OPENDATA
    jurisdictions: ClassVar[frozenset[str]] = frozenset(
        {"EU", "DE", "NL", "FR", "IE", "CH"}
    )
    capabilities: ClassVar[tuple[str, ...]] = (
        "dataset_search",
        "pagination",
        "facets",
        "resource_metadata",
    )

    def search(self, query: ExternalQuery) -> ProviderOutcome:
        q = default_search_query(
            query=query.query or "migration residence immigration",
            pathway_id=query.pathway_id,
            requirement=query.requirement,
            jurisdiction=query.jurisdiction,
        )
        limit = max(1, min(query.max_results_per_provider, 50))
        params: dict[str, Any] = {
            "q": q,
            "filter": "dataset",
            "limit": limit,
            "page": 0,
        }
        # Optional country facet when a member-state jurisdiction is given.
        if query.jurisdiction and query.jurisdiction.upper() not in {"EU"}:
            params["facets"] = json.dumps(
                {"country": [query.jurisdiction.lower()]},
                separators=(",", ":"),
            )

        resp = self.http.get_json(SEARCH_URL, params=params)
        if not resp.ok:
            return ProviderOutcome(
                provider=self.provider_id,
                status=resp.retrieval_status,
                error_detail=resp.error,
                capabilities=list(self.capabilities),
            )
        data = resp.json_data if isinstance(resp.json_data, dict) else {}
        result = data.get("result") if isinstance(data.get("result"), dict) else data
        results = result.get("results") if isinstance(result.get("results"), list) else []
        if not results:
            # Some deployments nest under result.results differently
            if isinstance(data.get("results"), list):
                results = data["results"]
        if not results:
            return ProviderOutcome(
                provider=self.provider_id,
                status=RetrievalStatus.NO_RESULTS,
                error_detail="No matching data.europa.eu datasets",
                capabilities=list(self.capabilities),
            )

        items: list[ExternalEvidence] = []
        for row in results[:limit]:
            if isinstance(row, dict):
                items.append(self._row_to_evidence(row, query))

        return ProviderOutcome(
            provider=self.provider_id,
            status=RetrievalStatus.OK,
            items=items,
            capabilities=list(self.capabilities),
        )

    def _row_to_evidence(
        self, row: dict[str, Any], query: ExternalQuery
    ) -> ExternalEvidence:
        title = _localized(row.get("title")) or str(row.get("id") or "dataset")
        description = _localized(row.get("description")) or ""
        ds_id = str(row.get("id") or row.get("uri") or title)
        landing = (
            str(row.get("resource") or "")
            or str(row.get("page") or "")
            or f"https://data.europa.eu/data/datasets/{ds_id}"
        )
        if landing and not landing.startswith("http"):
            landing = f"https://data.europa.eu/data/datasets/{ds_id}"
        distributions = row.get("distributions") or row.get("distribution") or []
        resource_meta = []
        if isinstance(distributions, list):
            for dist in distributions[:5]:
                if isinstance(dist, dict):
                    resource_meta.append(
                        {
                            "title": _localized(dist.get("title")),
                            "format": dist.get("format") or dist.get("mediaType"),
                            "access_url": dist.get("access_url")
                            or dist.get("download_url")
                            or dist.get("accessURL"),
                        }
                    )
        passages = extract_passages(
            f"{title}\n{description}\n"
            "Note: Open data catalog metadata does not establish national "
            "immigration eligibility rules."
        )
        return ExternalEvidence(
            provider=self.provider_id,
            title=title,
            official_url=landing,
            canonical_url=landing,
            document_id=ds_id,
            source_type=SourceType.DATASET,
            jurisdiction=query.jurisdiction or "EU",
            pathway_id=query.pathway_id,
            requirement=query.requirement,
            passages=passages,
            published_at=_as_str(row.get("issued") or row.get("release_date")),
            updated_at=_as_str(row.get("modified") or row.get("modification_date")),
            language=_first_lang(row.get("languages") or row.get("language")),
            license=_localized(row.get("license")) or _as_str(row.get("licence")),
            legal_status="dataset_catalog",
            metadata={
                "catalog": row.get("catalog") or row.get("catalogue"),
                "distributions": resource_meta,
                "warning": (
                    "Dataset metadata only — not national pathway eligibility evidence."
                ),
            },
        )


def _localized(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for lang in ("en", "eng", "en_GB", "de", "fr"):
            v = value.get(lang)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in value.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, list) and v and isinstance(v[0], str):
                return v[0].strip()
    if isinstance(value, list) and value:
        return _localized(value[0])
    return ""


def _first_lang(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return str(value[0])
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
