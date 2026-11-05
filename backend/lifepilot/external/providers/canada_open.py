"""Canada Open Government CKAN Action API connector.

Docs: https://open.canada.ca/en/access-our-application-programming-interface-api
Base: https://open.canada.ca/data/api/3/action/
GET-only. No API key for public package_search / package_show.
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

ACTION_BASE = "https://open.canada.ca/data/api/3/action"
ALLOWED_HOSTS = frozenset({"open.canada.ca"})


class CanadaOpenConnector(ExternalConnector):
    provider_id: ClassVar[ProviderId] = ProviderId.CANADA_OPEN
    jurisdictions: ClassVar[frozenset[str]] = frozenset({"CA"})
    capabilities: ClassVar[tuple[str, ...]] = (
        "package_search",
        "package_show",
        "pagination",
        "resource_metadata",
        "dataset_discovery",
    )

    def search(self, query: ExternalQuery) -> ProviderOutcome:
        q = default_search_query(
            query=query.query or "immigration",
            pathway_id=query.pathway_id,
            requirement=query.requirement,
            jurisdiction="Canada immigration",
        )
        # Prefer immigration-relevant terms; still a dataset catalog, not IRCC eligibility.
        rows = max(1, min(query.max_results_per_provider, 20))
        params: dict[str, Any] = {
            "q": q,
            "rows": rows,
            "start": 0,
        }
        resp = self.http.get_json(f"{ACTION_BASE}/package_search", params=params)
        if not resp.ok:
            return ProviderOutcome(
                provider=self.provider_id,
                status=resp.retrieval_status,
                error_detail=resp.error,
                capabilities=list(self.capabilities),
            )
        payload = resp.json_data if isinstance(resp.json_data, dict) else {}
        if not payload.get("success"):
            return ProviderOutcome(
                provider=self.provider_id,
                status=RetrievalStatus.INVALID_RESPONSE,
                error_detail="CKAN response success=false",
                capabilities=list(self.capabilities),
            )
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        packages = result.get("results") if isinstance(result.get("results"), list) else []
        if not packages:
            return ProviderOutcome(
                provider=self.provider_id,
                status=RetrievalStatus.NO_RESULTS,
                error_detail="No matching open.canada.ca datasets",
                capabilities=list(self.capabilities),
            )

        items: list[ExternalEvidence] = []
        for pkg in packages[:rows]:
            if not isinstance(pkg, dict):
                continue
            items.append(self._package_to_evidence(pkg, query))

        return ProviderOutcome(
            provider=self.provider_id,
            status=RetrievalStatus.OK,
            items=items,
            capabilities=list(self.capabilities),
        )

    def _package_to_evidence(
        self, pkg: dict[str, Any], query: ExternalQuery
    ) -> ExternalEvidence:
        name = str(pkg.get("name") or pkg.get("id") or "")
        title = _title(pkg.get("title")) or name
        notes = _title(pkg.get("notes")) or ""
        org = pkg.get("organization") if isinstance(pkg.get("organization"), dict) else {}
        resources = pkg.get("resources") if isinstance(pkg.get("resources"), list) else []
        resource_urls = []
        for res in resources[:5]:
            if isinstance(res, dict) and res.get("url"):
                resource_urls.append(
                    {
                        "id": res.get("id"),
                        "name": res.get("name"),
                        "format": res.get("format"),
                        "url": res.get("url"),
                    }
                )
        landing = f"https://open.canada.ca/data/en/dataset/{name}" if name else ""
        passages = extract_passages(
            f"{title}\n{notes}\n"
            "Note: Open Government datasets are not IRCC visa eligibility rules."
        )
        return ExternalEvidence(
            provider=self.provider_id,
            title=title,
            official_url=landing,
            canonical_url=landing,
            document_id=str(pkg.get("id") or name),
            source_type=SourceType.DATASET,
            jurisdiction="CA",
            pathway_id=query.pathway_id,
            requirement=query.requirement,
            passages=passages,
            published_at=_as_str(pkg.get("metadata_created")),
            updated_at=_as_str(pkg.get("metadata_modified")),
            language="en",
            license=_license(pkg),
            legal_status="dataset_catalog",
            metadata={
                "organization": org.get("title") or org.get("name"),
                "resources": resource_urls,
                "tags": [
                    t.get("name")
                    for t in (pkg.get("tags") or [])
                    if isinstance(t, dict) and t.get("name")
                ][:20],
                "warning": (
                    "Dataset metadata only — not individual immigration eligibility evidence."
                ),
            },
        )


def _title(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for lang in ("en", "fr", "en-CA", "fr-CA"):
            if isinstance(value.get(lang), str) and value[lang].strip():
                return value[lang].strip()
        for v in value.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _license(pkg: dict[str, Any]) -> str | None:
    for key in ("license_title", "license_id", "license_url"):
        val = pkg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return "Open Government Licence - Canada"


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
