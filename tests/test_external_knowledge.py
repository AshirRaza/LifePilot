"""Offline tests for Phase 7 external knowledge integrations."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from lifepilot.external.cache import ResponseCache
from lifepilot.external.http import ExternalHttpClient, HttpClientConfig
from lifepilot.external.mapping import (
    GOVUK_PATHWAY_PATHS,
    providers_for_jurisdiction,
    resolve_jurisdiction,
)
from lifepilot.external.models import (
    ExternalEvidence,
    ExternalQuery,
    ProviderId,
    RetrievalStatus,
    SourceType,
    VerificationState,
)
from lifepilot.external.providers.canada_open import CanadaOpenConnector
from lifepilot.external.providers.eu_immigration_portal import (
    EuImmigrationPortalConnector,
)
from lifepilot.external.providers.eu_opendata import EuOpenDataConnector
from lifepilot.external.providers.eurlex_cellar import EurlexCellarConnector
from lifepilot.external.providers.govuk import GovukContentConnector
from lifepilot.external.providers.us_federal_register import UsFederalRegisterConnector
from lifepilot.external.router import ExternalKnowledgeRouter, dedupe_evidence


def _json_response(url: str, payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", url),
        headers={"Content-Type": "application/json"},
    )


def _text_response(url: str, text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        text=text,
        request=httpx.Request("GET", url),
        headers={"Content-Type": "text/html"},
    )


def _client_with_handler(handler) -> ExternalHttpClient:
    transport = httpx.MockTransport(handler)
    raw = httpx.Client(transport=transport, follow_redirects=True)
    return ExternalHttpClient(
        config=HttpClientConfig(timeout_seconds=5.0, max_retries=1, backoff_base_seconds=0.01),
        cache=ResponseCache(default_ttl_seconds=60.0, disk_dir=None),
        client=raw,
        allowed_hosts=None,
    )


# ---------------------------------------------------------------------------
# Mapping / models
# ---------------------------------------------------------------------------


def test_resolve_jurisdiction_from_pathway():
    assert resolve_jurisdiction(None, "gb_skilled_worker") == "GB"
    assert resolve_jurisdiction("uk", None) == "GB"
    assert resolve_jurisdiction("CA", "us_h1b") == "CA"


def test_providers_for_jurisdiction_deterministic():
    assert ProviderId.GOVUK_CONTENT in providers_for_jurisdiction("GB")
    assert ProviderId.CANADA_OPEN in providers_for_jurisdiction("CA")
    assert ProviderId.US_FEDERAL_REGISTER in providers_for_jurisdiction("US")
    assert ProviderId.GOVUK_CONTENT not in providers_for_jurisdiction("US")


def test_external_evidence_defaults_unverified():
    item = ExternalEvidence(
        provider=ProviderId.GOVUK_CONTENT,
        title="t",
        official_url="https://www.gov.uk/x",
        source_type=SourceType.GUIDANCE,
    )
    assert item.verification_state == VerificationState.UNVERIFIED_EXTERNAL


def test_govuk_pathway_paths_cover_mvp_visas():
    for pathway in (
        "gb_skilled_worker",
        "gb_student",
        "gb_graduate",
        "gb_global_talent",
        "gb_visitor",
    ):
        assert pathway in GOVUK_PATHWAY_PATHS


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def test_http_cache_hit():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _json_response(str(request.url), {"ok": True})

    client = _client_with_handler(handler)
    r1 = client.get_json("https://example.test/api")
    r2 = client.get_json("https://example.test/api")
    assert r1.ok and r2.ok
    assert r2.from_cache is True
    assert calls["n"] == 1
    client.close()


def test_http_timeout_status():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = _client_with_handler(handler)
    resp = client.get_json("https://example.test/slow")
    assert resp.ok is False
    assert resp.retrieval_status == RetrievalStatus.TIMEOUT
    client.close()


def test_http_rate_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            request=request,
            headers={"Retry-After": "0"},
            text="slow down",
        )

    client = _client_with_handler(handler)
    resp = client.get_json("https://example.test/limited")
    assert resp.retrieval_status == RetrievalStatus.RATE_LIMITED
    client.close()


def test_http_malformed_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            text="not-json",
            headers={"Content-Type": "application/json"},
        )

    client = _client_with_handler(handler)
    resp = client.get_json("https://example.test/bad")
    assert resp.retrieval_status == RetrievalStatus.INVALID_RESPONSE
    client.close()


def test_http_host_block():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(str(request.url), {})

    transport = httpx.MockTransport(handler)
    raw = httpx.Client(transport=transport)
    client = ExternalHttpClient(
        client=raw,
        allowed_hosts=frozenset({"www.gov.uk"}),
        cache=ResponseCache(disk_dir=None),
    )
    resp = client.get_json("https://evil.example/api")
    assert resp.retrieval_status == RetrievalStatus.BLOCKED
    client.close()


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------


def test_govuk_successful_retrieval():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/api/content/skilled-worker-visa" in str(request.url)
        return _json_response(
            str(request.url),
            {
                "base_path": "/skilled-worker-visa",
                "content_id": "abc-123",
                "title": "Skilled Worker visa",
                "description": "Work in the UK",
                "document_type": "guide",
                "schema_name": "guide",
                "locale": "en",
                "first_published_at": "2020-01-01T00:00:00.000Z",
                "public_updated_at": "2024-01-01T00:00:00.000Z",
                "details": {
                    "body": (
                        "<p>You can apply for a Skilled Worker visa if you have "
                        "a job offer from a licensed sponsor and meet the skill "
                        "and salary requirements published on GOV.UK.</p>"
                    )
                },
            },
        )

    http = _client_with_handler(handler)
    outcome = GovukContentConnector(http).search(
        ExternalQuery(pathway_id="gb_skilled_worker", jurisdiction="GB")
    )
    assert outcome.status == RetrievalStatus.OK
    assert outcome.items[0].verification_state == VerificationState.UNVERIFIED_EXTERNAL
    assert outcome.items[0].jurisdiction == "GB"
    assert "Skilled Worker" in outcome.items[0].title
    assert outcome.items[0].passages
    http.close()


def test_govuk_missing_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request, text="missing")

    http = _client_with_handler(handler)
    outcome = GovukContentConnector(http).search(
        ExternalQuery(query="definitely-not-a-real-path-xyz")
    )
    assert outcome.status in {
        RetrievalStatus.NO_RESULTS,
        RetrievalStatus.TRANSPORT_ERROR,
    }
    http.close()


def test_canada_open_package_search():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "package_search" in str(request.url)
        return _json_response(
            str(request.url),
            {
                "success": True,
                "result": {
                    "count": 1,
                    "results": [
                        {
                            "id": "ds-1",
                            "name": "immigration-stats",
                            "title": {"en": "Immigration statistics"},
                            "notes": {"en": "Annual immigration admissions statistics dataset."},
                            "metadata_created": "2020-01-01",
                            "metadata_modified": "2024-01-01",
                            "license_title": "Open Government Licence - Canada",
                            "organization": {"title": "IRCC"},
                            "resources": [
                                {
                                    "id": "r1",
                                    "name": "csv",
                                    "format": "CSV",
                                    "url": "https://example/resource.csv",
                                }
                            ],
                            "tags": [{"name": "immigration"}],
                        }
                    ],
                },
            },
        )

    http = _client_with_handler(handler)
    outcome = CanadaOpenConnector(http).search(
        ExternalQuery(query="immigration", jurisdiction="CA")
    )
    assert outcome.status == RetrievalStatus.OK
    assert outcome.items[0].source_type == SourceType.DATASET
    assert "not IRCC visa eligibility" in " ".join(outcome.items[0].passages).lower() or (
        "eligibility" in json.dumps(outcome.items[0].metadata).lower()
    )
    http.close()


def test_canada_open_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            str(request.url),
            {"success": True, "result": {"count": 0, "results": []}},
        )

    http = _client_with_handler(handler)
    outcome = CanadaOpenConnector(http).search(ExternalQuery(query="zzzz-no-match"))
    assert outcome.status == RetrievalStatus.NO_RESULTS
    http.close()


def test_eu_opendata_search():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            str(request.url),
            {
                "result": {
                    "results": [
                        {
                            "id": "eu-migr-1",
                            "title": {"en": "Migration statistics"},
                            "description": {
                                "en": "Eurostat migration dataset metadata for research use."
                            },
                            "issued": "2021-01-01",
                            "modified": "2024-01-01",
                            "distributions": [
                                {
                                    "title": {"en": "TSV"},
                                    "format": "TSV",
                                    "access_url": "https://example/data.tsv",
                                }
                            ],
                        }
                    ]
                }
            },
        )

    http = _client_with_handler(handler)
    outcome = EuOpenDataConnector(http).search(
        ExternalQuery(query="migration", jurisdiction="EU")
    )
    assert outcome.status == RetrievalStatus.OK
    assert outcome.items[0].source_type == SourceType.DATASET
    http.close()


def test_eurlex_known_celex_and_html():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "sparql" in url:
            return _json_response(
                url,
                {
                    "results": {
                        "bindings": [
                            {
                                "work": {
                                    "value": "http://publications.europa.eu/resource/cellar/x"
                                },
                                "title": {"value": "Blue Card Directive"},
                                "date": {"value": "2021-10-20"},
                                "type": {
                                    "value": "http://publications.europa.eu/ontology/cdm#directive"
                                },
                            }
                        ]
                    }
                },
            )
        if "celex" in url.lower():
            return _text_response(
                url,
                "<html><body><p>This Directive lays down the conditions of entry "
                "and residence of third-country nationals for the purpose of "
                "highly qualified employment in the European Union member states.</p>"
                "</body></html>",
            )
        return httpx.Response(404, request=request)

    http = _client_with_handler(handler)
    outcome = EurlexCellarConnector(http).search(
        ExternalQuery(query="blue card", jurisdiction="EU", max_results_per_provider=1)
    )
    assert outcome.status == RetrievalStatus.OK
    assert outcome.items[0].document_id == "32021L1883"
    assert outcome.items[0].source_type == SourceType.LEGISLATION
    assert "directive" in (outcome.items[0].legal_status or "")
    http.close()


def test_federal_register_search_and_types():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            str(request.url),
            {
                "count": 2,
                "results": [
                    {
                        "title": "H-1B Cap Proposed Rule",
                        "type": "Proposed Rule",
                        "abstract": "USCIS proposes changes to the H-1B selection process.",
                        "document_number": "2024-0001",
                        "html_url": "https://www.federalregister.gov/documents/2024-0001",
                        "publication_date": "2024-01-15",
                        "agencies": [{"name": "USCIS", "slug": "u-s-citizenship-and-immigration-services"}],
                    },
                    {
                        "title": "Final Rule on Asylum",
                        "type": "Rule",
                        "abstract": "DHS issues a final rule affecting asylum procedures.",
                        "document_number": "2024-0002",
                        "html_url": "https://www.federalregister.gov/documents/2024-0002",
                        "publication_date": "2024-02-01",
                        "effective_on": "2024-03-01",
                        "agencies": [{"name": "DHS"}],
                    },
                ],
            },
        )

    http = _client_with_handler(handler)
    outcome = UsFederalRegisterConnector(http).search(
        ExternalQuery(query="immigration", jurisdiction="US", max_results_per_provider=5)
    )
    assert outcome.status == RetrievalStatus.OK
    assert len(outcome.items) == 2
    assert outcome.items[0].source_type == SourceType.PROPOSED_RULE
    assert outcome.items[1].source_type == SourceType.FINAL_RULE
    assert outcome.items[0].legal_status == "proposed_not_effective"
    http.close()


def test_eu_immigration_portal_owner_action():
    def handler(request: httpx.Request) -> httpx.Response:
        return _text_response(str(request.url), "<html><body>EU Immigration Portal</body></html>")

    http = _client_with_handler(handler)
    outcome = EuImmigrationPortalConnector(http).search(
        ExternalQuery(jurisdiction="EU", query="work")
    )
    assert outcome.status == RetrievalStatus.OWNER_ACTION_REQUIRED
    assert outcome.items[0].passages == []
    assert outcome.items[0].metadata.get("public_api") is False
    http.close()


def test_eu_immigration_portal_unavailable_when_down():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, text="down")

    http = _client_with_handler(handler)
    outcome = EuImmigrationPortalConnector(http).search(ExternalQuery(jurisdiction="EU"))
    assert outcome.status == RetrievalStatus.UNAVAILABLE
    http.close()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_router_partial_failure_and_dedupe():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "gov.uk" in url:
            return _json_response(
                url,
                {
                    "base_path": "/student-visa",
                    "content_id": "stu-1",
                    "title": "Student visa",
                    "description": "Study in the UK with a Student visa under the points-based system.",
                    "locale": "en",
                    "details": {
                        "body": "<p>You must have an offer from a licensed student sponsor "
                        "and meet English language and financial requirements.</p>"
                    },
                },
            )
        if "federalregister" in url:
            raise httpx.ConnectError("boom", request=request)
        if "home-affairs" in url or "immigration-portal" in url:
            return _text_response(url, "<html>portal</html>")
        if "open.canada" in url:
            return _json_response(url, {"success": True, "result": {"results": []}})
        if "data.europa.eu" in url:
            return _json_response(url, {"result": {"results": []}})
        if "publications.europa.eu" in url:
            return _json_response(url, {"results": {"bindings": []}})
        return httpx.Response(404, request=request)

    http = _client_with_handler(handler)
    router = ExternalKnowledgeRouter(http=http, enable_disk_cache=False)
    # Explicit multi-provider selection including a failing US provider
    result = router.query(
        ExternalQuery(
            pathway_id="gb_student",
            jurisdiction="GB",
            providers=(
                ProviderId.GOVUK_CONTENT,
                ProviderId.US_FEDERAL_REGISTER,
            ),
        )
    )
    gov = next(o for o in result.outcomes if o.provider == ProviderId.GOVUK_CONTENT)
    us = next(o for o in result.outcomes if o.provider == ProviderId.US_FEDERAL_REGISTER)
    assert gov.status == RetrievalStatus.OK
    assert us.status in {
        RetrievalStatus.TRANSPORT_ERROR,
        RetrievalStatus.TIMEOUT,
    }
    assert result.items
    assert all(
        i.verification_state == VerificationState.UNVERIFIED_EXTERNAL for i in result.items
    )
    router.close()


def test_router_skips_irrelevant_providers_by_jurisdiction():
    called = {"urls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        called["urls"].append(str(request.url))
        url = str(request.url)
        if "gov.uk" in url:
            return _json_response(
                url,
                {
                    "base_path": "/skilled-worker-visa",
                    "content_id": "x",
                    "title": "Skilled Worker visa",
                    "description": "Work route guidance for skilled workers with a sponsor.",
                    "details": {"body": "<p>Eligibility includes a job offer from a licensed sponsor.</p>"},
                },
            )
        return httpx.Response(500, request=request, text="should not be called")

    http = _client_with_handler(handler)
    router = ExternalKnowledgeRouter(http=http, enable_disk_cache=False)
    result = router.query(
        ExternalQuery(pathway_id="gb_skilled_worker", jurisdiction="GB", max_results_per_provider=1)
    )
    providers = {o.provider for o in result.outcomes}
    assert ProviderId.GOVUK_CONTENT in providers
    assert ProviderId.US_FEDERAL_REGISTER not in providers
    assert ProviderId.CANADA_OPEN not in providers
    assert all("federalregister.gov" not in u for u in called["urls"])
    router.close()


def test_dedupe_evidence():
    a = ExternalEvidence(
        provider=ProviderId.GOVUK_CONTENT,
        title="A",
        official_url="https://www.gov.uk/x",
        canonical_url="https://www.gov.uk/x",
        document_id="1",
        source_type=SourceType.GUIDANCE,
    )
    b = ExternalEvidence(
        provider=ProviderId.GOVUK_CONTENT,
        title="A copy",
        official_url="https://www.gov.uk/x",
        canonical_url="https://www.gov.uk/x",
        document_id="1",
        source_type=SourceType.GUIDANCE,
    )
    assert len(dedupe_evidence([a, b])) == 1


def test_authorization_unchanged_helpers_still_importable():
    """Phase 7 must not weaken crawl allowlist helpers."""
    from lifepilot.registry.urls import url_in_any_allowed_pattern

    assert url_in_any_allowed_pattern(
        "https://www.gov.uk/skilled-worker-visa",
        ["https://www.gov.uk/skilled-worker-visa"],
    )
    assert not url_in_any_allowed_pattern(
        "https://www.gov.uk/other",
        ["https://www.gov.uk/skilled-worker-visa"],
    )


@pytest.mark.skipif(
    not __import__("os").environ.get("LIFEPILOT_LIVE_EXTERNAL"),
    reason="Set LIFEPILOT_LIVE_EXTERNAL=1 to run live external smoke tests",
)
def test_live_govuk_smoke():
    """Optional live smoke — off by default for the offline suite."""
    with ExternalKnowledgeRouter(enable_disk_cache=False) as router:
        result = router.query(
            ExternalQuery(
                pathway_id="gb_skilled_worker",
                providers=(ProviderId.GOVUK_CONTENT,),
                max_results_per_provider=1,
            )
        )
    outcome = result.outcomes[0]
    if outcome.status != RetrievalStatus.OK:
        pytest.skip(f"Live GOV.UK unavailable: {outcome.error_detail}")
    assert result.items[0].official_url.startswith("https://www.gov.uk/")
