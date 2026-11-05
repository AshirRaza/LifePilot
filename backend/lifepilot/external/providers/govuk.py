"""GOV.UK Content API connector.

Docs: https://content-api.publishing.service.gov.uk/
Endpoint: GET https://www.gov.uk/api/content/{path}
No API key required.
"""

from __future__ import annotations

from typing import Any, ClassVar

from lifepilot.external.base import ExternalConnector
from lifepilot.external.mapping import GOVUK_PATHWAY_PATHS, GOVUK_REQUIREMENT_HINTS
from lifepilot.external.models import (
    ExternalEvidence,
    ExternalQuery,
    ProviderId,
    ProviderOutcome,
    RetrievalStatus,
    SourceType,
)
from lifepilot.external.textutil import dig_body_html, extract_passages, html_to_text

BASE = "https://www.gov.uk/api/content"
ALLOWED_HOSTS = frozenset({"www.gov.uk"})


class GovukContentConnector(ExternalConnector):
    provider_id: ClassVar[ProviderId] = ProviderId.GOVUK_CONTENT
    jurisdictions: ClassVar[frozenset[str]] = frozenset({"GB", "UK"})
    capabilities: ClassVar[tuple[str, ...]] = (
        "content_by_path",
        "pathway_guidance",
        "redirect_follow",
        "structured_json",
    )

    def search(self, query: ExternalQuery) -> ProviderOutcome:
        paths = self._candidate_paths(query)
        if not paths:
            return ProviderOutcome(
                provider=self.provider_id,
                status=RetrievalStatus.NO_RESULTS,
                error_detail="No GOV.UK path could be derived from the query",
                capabilities=list(self.capabilities),
            )

        items: list[ExternalEvidence] = []
        last_error: str | None = None
        last_status = RetrievalStatus.NO_RESULTS
        limit = max(1, query.max_results_per_provider)

        for path in paths:
            if len(items) >= limit:
                break
            resp = self.http.get_json(f"{BASE}/{path.lstrip('/')}")
            if not resp.ok:
                last_status = resp.retrieval_status
                last_error = resp.error
                # 303 should already be followed by httpx; treat empty as miss.
                continue
            data = resp.json_data
            if not isinstance(data, dict):
                last_status = RetrievalStatus.INVALID_RESPONSE
                last_error = "GOV.UK response was not a JSON object"
                continue
            evidence = self._to_evidence(data, query, requested_path=path)
            # Dedupe by content_id / base_path
            if any(e.document_id == evidence.document_id for e in items):
                continue
            items.append(evidence)

        if items:
            return ProviderOutcome(
                provider=self.provider_id,
                status=RetrievalStatus.OK,
                items=items[:limit],
                capabilities=list(self.capabilities),
            )
        return ProviderOutcome(
            provider=self.provider_id,
            status=last_status,
            error_detail=last_error or "No GOV.UK content found",
            capabilities=list(self.capabilities),
        )

    def _candidate_paths(self, query: ExternalQuery) -> list[str]:
        paths: list[str] = []
        if query.pathway_id and query.pathway_id in GOVUK_PATHWAY_PATHS:
            base = GOVUK_PATHWAY_PATHS[query.pathway_id]
            paths.append(base)
            if query.requirement:
                for hint in GOVUK_REQUIREMENT_HINTS.get(query.requirement, []):
                    paths.append(f"{base}/{hint}")
        # Allow explicit path-like queries (e.g. "skilled-worker-visa")
        q = (query.query or "").strip().strip("/")
        if q and " " not in q and not q.startswith("http"):
            paths.append(q)
        # Keyword fallbacks for common visas when no pathway mapped
        keywords = {
            "skilled worker": "skilled-worker-visa",
            "student visa": "student-visa",
            "graduate visa": "graduate-visa",
            "global talent": "global-talent",
            "standard visitor": "standard-visitor",
            "visitor": "standard-visitor",
        }
        lowered = (query.query or "").lower()
        for needle, path in keywords.items():
            if needle in lowered:
                paths.append(path)
        # Stable unique order
        seen: set[str] = set()
        out: list[str] = []
        for p in paths:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def _to_evidence(
        self,
        data: dict[str, Any],
        query: ExternalQuery,
        *,
        requested_path: str,
    ) -> ExternalEvidence:
        base_path = data.get("base_path") or f"/{requested_path}"
        official = f"https://www.gov.uk{base_path}"
        details = data.get("details") if isinstance(data.get("details"), dict) else {}
        body_html = dig_body_html(details) or ""
        description = data.get("description") if isinstance(data.get("description"), str) else ""
        text = html_to_text(body_html) if body_html else description
        passages = extract_passages(text)
        withdrawn = data.get("withdrawn_notice")
        legal_status = None
        if isinstance(withdrawn, dict) and withdrawn:
            legal_status = "withdrawn"
        return ExternalEvidence(
            provider=self.provider_id,
            title=str(data.get("title") or base_path),
            official_url=official,
            canonical_url=official,
            document_id=str(data.get("content_id") or base_path),
            source_type=SourceType.GUIDANCE,
            jurisdiction="GB",
            pathway_id=query.pathway_id,
            requirement=query.requirement,
            passages=passages,
            published_at=_as_str(data.get("first_published_at")),
            updated_at=_as_str(data.get("public_updated_at") or data.get("updated_at")),
            language=_as_str(data.get("locale")),
            license="Open Government Licence v3.0",
            legal_status=legal_status,
            metadata={
                "document_type": data.get("document_type"),
                "schema_name": data.get("schema_name"),
                "requested_path": requested_path,
                "phase": data.get("phase"),
            },
        )


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
