"""Deterministic external knowledge router (no LLM routing)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from lifepilot.external.cache import ResponseCache
from lifepilot.external.http import ExternalHttpClient, HttpClientConfig
from lifepilot.external.mapping import providers_for_jurisdiction, resolve_jurisdiction
from lifepilot.external.models import (
    ExternalEvidence,
    ExternalQuery,
    ProviderId,
    ProviderOutcome,
    RetrievalResult,
    RetrievalStatus,
)
from lifepilot.external.providers import ALL_CONNECTOR_CLASSES
from lifepilot.external.providers.canada_open import ALLOWED_HOSTS as CA_HOSTS
from lifepilot.external.providers.eu_immigration_portal import (
    ALLOWED_HOSTS as EIP_HOSTS,
)
from lifepilot.external.providers.eu_opendata import ALLOWED_HOSTS as EU_OD_HOSTS
from lifepilot.external.providers.eurlex_cellar import ALLOWED_HOSTS as CELLAR_HOSTS
from lifepilot.external.providers.govuk import ALLOWED_HOSTS as GOVUK_HOSTS
from lifepilot.external.providers.national_official import (
    ALLOWED_HOSTS as NATIONAL_HOSTS,
)
from lifepilot.external.providers.us_federal_register import (
    ALLOWED_HOSTS as FR_HOSTS,
)

# backend/lifepilot/external/router.py → repo root is parents[3]
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache" / "external"

ALL_ALLOWED_HOSTS = (
    GOVUK_HOSTS
    | CA_HOSTS
    | EU_OD_HOSTS
    | CELLAR_HOSTS
    | FR_HOSTS
    | EIP_HOSTS
    | NATIONAL_HOSTS
)


class ExternalKnowledgeRouter:
    """Select providers, run connectors, normalize/dedupe partial results."""

    def __init__(
        self,
        *,
        http: ExternalHttpClient | None = None,
        cache: ResponseCache | None = None,
        enable_disk_cache: bool = True,
    ) -> None:
        if cache is None and enable_disk_cache:
            cache = ResponseCache(
                default_ttl_seconds=3600.0,
                disk_dir=DEFAULT_CACHE_DIR,
            )
        elif cache is None:
            cache = ResponseCache(default_ttl_seconds=3600.0, disk_dir=None)
        self.cache = cache
        self._owns_http = http is None
        self.http = http or ExternalHttpClient(
            config=HttpClientConfig(),
            cache=self.cache,
            allowed_hosts=ALL_ALLOWED_HOSTS,
        )
        self._connectors = {
            cls.provider_id: cls(self.http) for cls in ALL_CONNECTOR_CLASSES
        }

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> ExternalKnowledgeRouter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def provider_status(self) -> list[dict]:
        out = []
        for pid, connector in self._connectors.items():
            out.append(
                {
                    "provider": pid.value,
                    "jurisdictions": sorted(connector.jurisdictions),
                    "capabilities": list(connector.capabilities),
                }
            )
        return out

    def query(self, request: ExternalQuery) -> RetrievalResult:
        jurisdiction = resolve_jurisdiction(request.jurisdiction, request.pathway_id)
        normalized = ExternalQuery(
            query=request.query,
            jurisdiction=jurisdiction,
            pathway_id=request.pathway_id,
            requirement=request.requirement,
            providers=request.providers,
            published_after=request.published_after,
            published_before=request.published_before,
            max_results_per_provider=max(1, min(request.max_results_per_provider, 50)),
            language=request.language,
        )

        explicit = bool(normalized.providers)
        selected = self._select_providers(normalized)
        outcomes: list[ProviderOutcome] = []
        for pid in selected:
            connector = self._connectors[pid]
            try:
                # Auto-routing skips irrelevant jurisdictions; explicit
                # --provider selections always execute (for diagnostics).
                if (
                    not explicit
                    and not connector.supports(normalized)
                    and normalized.jurisdiction
                ):
                    outcomes.append(
                        ProviderOutcome(
                            provider=pid,
                            status=RetrievalStatus.NO_RESULTS,
                            error_detail=(
                                f"Provider not applicable for jurisdiction "
                                f"{normalized.jurisdiction}"
                            ),
                            capabilities=list(connector.capabilities),
                        )
                    )
                    continue
                outcomes.append(connector.search(normalized))
            except Exception as exc:  # noqa: BLE001 — isolate provider failures
                outcomes.append(
                    ProviderOutcome(
                        provider=pid,
                        status=RetrievalStatus.TRANSPORT_ERROR,
                        error_detail=f"Connector raised: {exc}",
                        capabilities=list(connector.capabilities),
                    )
                )

        items = dedupe_evidence(
            item for outcome in outcomes for item in outcome.items
        )
        return RetrievalResult(query=normalized, outcomes=outcomes, items=items)

    def _select_providers(self, request: ExternalQuery) -> list[ProviderId]:
        if request.providers:
            return [p for p in request.providers if p in self._connectors]
        return providers_for_jurisdiction(request.jurisdiction)


def dedupe_evidence(items: Iterable[ExternalEvidence]) -> list[ExternalEvidence]:
    seen: set[str] = set()
    out: list[ExternalEvidence] = []
    for item in items:
        key = item.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def query_external(
    *,
    query: str = "",
    jurisdiction: str | None = None,
    pathway_id: str | None = None,
    requirement: str | None = None,
    providers: tuple[ProviderId, ...] | None = None,
    published_after: str | None = None,
    published_before: str | None = None,
    max_results_per_provider: int = 5,
    router: ExternalKnowledgeRouter | None = None,
) -> RetrievalResult:
    """Convenience entry point for future RAG/agent tool calls."""
    owns = router is None
    router = router or ExternalKnowledgeRouter()
    try:
        return router.query(
            ExternalQuery(
                query=query,
                jurisdiction=jurisdiction,
                pathway_id=pathway_id,
                requirement=requirement,
                providers=providers,
                published_after=published_after,
                published_before=published_before,
                max_results_per_provider=max_results_per_provider,
            )
        )
    finally:
        if owns:
            router.close()
