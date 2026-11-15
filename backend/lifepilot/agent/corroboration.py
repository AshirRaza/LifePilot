"""Bounded official corroboration using existing national connectors.

At most one connector search per verification pass. Never unrestricted search.
Fetched snippets remain unverified_external and are not KB-promoted.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lifepilot.agent.evidence import EvidenceItem, evidence_from_external_item
from lifepilot.external.models import ExternalQuery, ProviderId

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CATALOG = ROOT / "config" / "external" / "national_official_sources.json"


@lru_cache(maxsize=1)
def _load_catalog() -> list[dict[str, Any]]:
    if not DEFAULT_CATALOG.exists():
        return []
    raw = json.loads(DEFAULT_CATALOG.read_text(encoding="utf-8"))
    return list(raw.get("sources") or [])


def catalog_urls_for(
    *,
    jurisdiction: str,
    pathway_id: str | None,
) -> list[str]:
    jur = (jurisdiction or "").strip().upper()
    pid = (pathway_id or "").strip() or None
    urls: list[str] = []
    for src in _load_catalog():
        if (src.get("jurisdiction") or "").upper() != jur:
            continue
        src_pid = (src.get("pathway_id") or "").strip() or None
        if pid and src_pid and src_pid != pid:
            continue
        if pid and not src_pid:
            continue
        url = (src.get("url") or "").strip()
        if url and url not in urls:
            urls.append(url)
    if not urls:
        for src in _load_catalog():
            if (src.get("jurisdiction") or "").upper() != jur:
                continue
            url = (src.get("url") or "").strip()
            if url and url not in urls:
                urls.append(url)
    return urls


class BoundedNationalCorroborator:
    """Fetch at most one allowlisted national official search for corroboration."""

    def __init__(self) -> None:
        self._calls = 0

    @property
    def calls(self) -> int:
        return self._calls

    def corroborate(
        self,
        *,
        claim: str,
        jurisdiction: str,
        pathway_id: str | None,
        existing_evidence: list[EvidenceItem],
    ) -> list[EvidenceItem]:
        jur = (jurisdiction or "").strip().upper()
        if jur not in {"NL", "IE", "FR", "SG"}:
            return []
        existing_hosts = {
            (urlparse(e.source_url).hostname or "").lower()
            for e in existing_evidence
            if e.source_url
        }
        # Skip when catalog pathway URL host already present in evidence.
        catalog = catalog_urls_for(jurisdiction=jur, pathway_id=pathway_id)
        if catalog:
            host = (urlparse(catalog[0]).hostname or "").lower()
            # Still allow fetch for distinct pages on same host; connector selects.
            _ = host, existing_hosts

        try:
            from lifepilot.external.providers.national_official import (
                NationalOfficialConnector,
            )

            connector = NationalOfficialConnector()
            query = ExternalQuery(
                query=claim[:240],
                jurisdiction=jur,
                pathway_id=pathway_id,
                requirement="corroboration",
                max_results_per_provider=2,
            )
            outcome = connector.search(query)
            self._calls += 1
            items: list[EvidenceItem] = []
            for hit in outcome.items or []:
                for passage in (hit.passages or [])[:2]:
                    text = (passage or "").strip()
                    if not text:
                        continue
                    try:
                        items.append(
                            evidence_from_external_item(
                                jurisdiction=jur,
                                official_url=hit.official_url,
                                provider=ProviderId.NATIONAL_OFFICIAL.value,
                                passage_text=text,
                                document_id=hit.document_id,
                                title=hit.title,
                                source_type_label=hit.source_type.value
                                if hit.source_type
                                else None,
                            )
                        )
                    except Exception:  # noqa: BLE001
                        continue
            return items[:3]
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "corroboration.national_fetch_skipped error=%s",
                exc.__class__.__name__,
            )
            self._calls += 1
            return []
