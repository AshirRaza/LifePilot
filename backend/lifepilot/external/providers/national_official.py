"""National official-source connector for NL, IE, FR, and SG.

Fetches curated allowlisted government HTML pages (not unrestricted search).
Evidence remains unverified_external and is never auto-promoted into the KB.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

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

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CATALOG = ROOT / "config" / "external" / "national_official_sources.json"

ALLOWED_HOSTS = frozenset(
    {
        "ind.nl",
        "www.ind.nl",
        "enterprise.gov.ie",
        "www.enterprise.gov.ie",
        "www.service-public.fr",
        "service-public.fr",
        "www.service-public.gouv.fr",
        "service-public.gouv.fr",
        "www.mom.gov.sg",
        "mom.gov.sg",
    }
)


@lru_cache(maxsize=1)
def load_national_catalog(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_CATALOG
    if not p.exists():
        return {"sources": []}
    return json.loads(p.read_text(encoding="utf-8"))


def select_national_sources(
    *,
    jurisdiction: str,
    pathway_id: str | None,
    query: str,
    catalog: dict[str, Any] | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Rank curated official URLs for a national jurisdiction request."""
    jur = (jurisdiction or "").strip().upper()
    if jur == "UK":
        jur = "GB"
    cat = catalog or load_national_catalog()
    q = (query or "").lower()
    pid = (pathway_id or "").strip() or None
    scored: list[tuple[int, dict[str, Any]]] = []
    for src in cat.get("sources") or []:
        if (src.get("jurisdiction") or "").upper() != jur:
            continue
        host = (urlparse(src.get("url") or "").hostname or "").lower()
        allowed = {h.lower() for h in (src.get("hosts") or [])} | set(ALLOWED_HOSTS)
        if host not in allowed:
            continue
        score = 0
        src_pid = (src.get("pathway_id") or "").strip() or None
        if pid and src_pid == pid:
            score += 100
        elif pid and src_pid and src_pid != pid:
            continue
        for kw in src.get("keywords") or []:
            if kw.lower() in q:
                score += 10
        if src_pid is None:
            score += 1  # hub pages as weak fallback
        if score > 0 or (pid is None and not q):
            scored.append((score, src))
    scored.sort(key=lambda t: (-t[0], t[1].get("id") or ""))
    out = [s for sc, s in scored if sc > 0][:limit]
    if not out and scored:
        out = [scored[0][1]]
    return out


class NationalOfficialConnector(ExternalConnector):
    provider_id: ClassVar[ProviderId] = ProviderId.NATIONAL_OFFICIAL
    jurisdictions: ClassVar[frozenset[str]] = frozenset({"NL", "IE", "FR", "SG"})
    capabilities: ClassVar[tuple[str, ...]] = (
        "curated_official_html",
        "pathway_pages",
        "allowlisted_hosts_only",
    )

    def search(self, query: ExternalQuery) -> ProviderOutcome:
        jur = (query.jurisdiction or "").strip().upper()
        if jur == "UK":
            jur = "GB"
        if jur not in self.jurisdictions:
            return ProviderOutcome(
                provider=self.provider_id,
                status=RetrievalStatus.NO_RESULTS,
                error_detail=f"jurisdiction {jur} not served by national_official",
                capabilities=list(self.capabilities),
            )

        candidates = select_national_sources(
            jurisdiction=jur,
            pathway_id=query.pathway_id,
            query=query.query or "",
            limit=max(1, min(query.max_results_per_provider, 3)),
        )
        if not candidates:
            return ProviderOutcome(
                provider=self.provider_id,
                status=RetrievalStatus.NO_RESULTS,
                error_detail="No curated national official URL matched the query",
                capabilities=list(self.capabilities),
            )

        items: list[ExternalEvidence] = []
        last_status = RetrievalStatus.NO_RESULTS
        last_error: str | None = None
        saw_blocked = False

        for src in candidates:
            url = src["url"]
            host = (urlparse(url).hostname or "").lower()
            if host not in ALLOWED_HOSTS:
                saw_blocked = True
                last_status = RetrievalStatus.BLOCKED
                last_error = f"host not allowlisted: {host}"
                continue
            # Reject redirects off allowlist via final_url check after fetch.
            resp = self.http.get_text(url, use_cache=True)
            if not resp.ok or not resp.text:
                if not saw_blocked:
                    last_status = resp.retrieval_status
                    last_error = resp.error or "empty response"
                continue
            final_host = (urlparse(resp.final_url or url).hostname or "").lower()
            if final_host not in ALLOWED_HOSTS:
                saw_blocked = True
                last_status = RetrievalStatus.BLOCKED
                last_error = f"redirect host not allowlisted: {final_host}"
                continue
            text = html_to_text(resp.text)
            passages = extract_passages(text, max_passages=6, max_chars=700)
            if not passages:
                last_status = RetrievalStatus.INVALID_RESPONSE
                last_error = "no usable text extracted"
                continue
            items.append(
                ExternalEvidence(
                    provider=self.provider_id,
                    title=str(src.get("title") or src.get("id") or "Official guidance"),
                    official_url=resp.final_url or url,
                    canonical_url=url,
                    source_type=SourceType.GUIDANCE,
                    jurisdiction=jur,
                    pathway_id=src.get("pathway_id") or query.pathway_id,
                    document_id=str(src.get("id")),
                    passages=passages,
                    language=None,
                    metadata={
                        "catalog_id": src.get("id"),
                        "from_cache": resp.from_cache,
                        "retrieval_channel": "national_official_html",
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
            status=(
                RetrievalStatus.BLOCKED if saw_blocked else last_status
            ),
            error_detail=last_error or "national official retrieval failed",
            capabilities=list(self.capabilities),
        )
