"""EU Immigration Portal connector.

Portal page:
https://home-affairs.ec.europa.eu/policies/migration-and-asylum/eu-immigration-portal_en

Investigation summary (Phase 7):
- No documented public API was found for the portal content.
- No officially advertised downloadable structured feed of pathway guidance.
- Related Eurostat/open-data statistics are a different product and must not be
  silently substituted as EU Immigration Portal guidance.
- HTML scraping of the CMS would require owner-approved exact URLs and must
  respect robots/terms; this connector does not scrape by default.

Therefore the connector reports OWNER_ACTION_REQUIRED / UNAVAILABLE accurately.
"""

from __future__ import annotations

from typing import ClassVar

from lifepilot.external.base import ExternalConnector
from lifepilot.external.models import (
    ExternalEvidence,
    ExternalQuery,
    ProviderId,
    ProviderOutcome,
    RetrievalStatus,
    SourceType,
)

PORTAL_URL = (
    "https://home-affairs.ec.europa.eu/policies/migration-and-asylum/"
    "eu-immigration-portal_en"
)
ALLOWED_HOSTS = frozenset({"home-affairs.ec.europa.eu", "immigration-portal.ec.europa.eu"})


class EuImmigrationPortalConnector(ExternalConnector):
    provider_id: ClassVar[ProviderId] = ProviderId.EU_IMMIGRATION_PORTAL
    jurisdictions: ClassVar[frozenset[str]] = frozenset(
        {"EU", "DE", "NL", "FR", "IE"}
    )
    capabilities: ClassVar[tuple[str, ...]] = (
        "status_probe",
        "no_public_api",
    )

    def search(self, query: ExternalQuery) -> ProviderOutcome:
        """Probe portal reachability; never fabricate guidance content."""
        # Optional reachability probe (HEAD/GET) — does not extract pathway rules.
        resp = self.http.get_text(PORTAL_URL, use_cache=True, cache_ttl=86400.0)
        reachable = bool(resp.ok and resp.status_code and resp.status_code < 400)

        detail = (
            "EU Immigration Portal has no documented public API or structured "
            "guidance feed. Owner action required: approve exact portal guidance "
            f"URLs for read-only retrieval (portal landing: {PORTAL_URL}). "
            "Do not substitute Eurostat statistics as portal eligibility guidance."
        )
        if not reachable:
            detail = (
                f"Portal landing page probe failed ({resp.error or resp.status_code}). "
                + detail
            )
            status = RetrievalStatus.UNAVAILABLE
        else:
            status = RetrievalStatus.OWNER_ACTION_REQUIRED

        stub = ExternalEvidence(
            provider=self.provider_id,
            title="EU Immigration Portal (no public API)",
            official_url=PORTAL_URL,
            canonical_url=PORTAL_URL,
            document_id="eu-immigration-portal",
            source_type=SourceType.GUIDANCE,
            jurisdiction="EU",
            pathway_id=query.pathway_id,
            requirement=query.requirement,
            passages=[],
            language="en",
            license="European Commission website reuse / copyright terms apply",
            legal_status="portal_html_only",
            retrieval_status=status,
            error_detail=detail,
            metadata={
                "public_api": False,
                "structured_feed": False,
                "landing_reachable": reachable,
                "owner_action": (
                    "Approve exact guidance page URLs under home-affairs.ec.europa.eu "
                    "or immigration-portal.ec.europa.eu for bounded read-only fetch."
                ),
            },
        )
        return ProviderOutcome(
            provider=self.provider_id,
            status=status,
            items=[stub],
            error_detail=detail,
            capabilities=list(self.capabilities),
        )
