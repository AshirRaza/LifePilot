"""National official-source connector and routing tests (mocked HTTP)."""

from __future__ import annotations

from lifepilot.agent.orchestration import explicit_providers_for_jurisdiction
from lifepilot.external.http import ExternalHttpClient, HttpResponse
from lifepilot.external.models import ExternalQuery, ProviderId, RetrievalStatus
from lifepilot.external.providers.national_official import (
    ALLOWED_HOSTS,
    NationalOfficialConnector,
    select_national_sources,
)


class _FakeHttp(ExternalHttpClient):
    def __init__(self, responses: dict[str, HttpResponse]):
        super().__init__(allowed_hosts=ALLOWED_HOSTS)
        self._responses = responses
        self.calls: list[str] = []

    def get_text(self, url: str, **kwargs):  # type: ignore[override]
        self.calls.append(url)
        return self._responses.get(
            url,
            HttpResponse(
                ok=False,
                status_code=404,
                url=url,
                final_url=url,
                error="missing fixture",
                retrieval_status=RetrievalStatus.NO_RESULTS,
            ),
        )


def test_national_routing_maps_nl_ie_fr_sg():
    for jur in ("NL", "IE", "FR", "SG"):
        providers = explicit_providers_for_jurisdiction(jur)
        assert ProviderId.NATIONAL_OFFICIAL in providers
        assert providers[0] == ProviderId.NATIONAL_OFFICIAL


def test_sg_has_national_not_only_empty():
    assert explicit_providers_for_jurisdiction("SG") == [ProviderId.NATIONAL_OFFICIAL]


def test_pathway_source_selection_nl_hsm():
    selected = select_national_sources(
        jurisdiction="NL",
        pathway_id="nl_highly_skilled_migrant",
        query="salary criterion",
    )
    assert selected
    assert selected[0]["id"] == "nl_hsm"


def test_pathway_source_selection_fr_passeport_talent():
    selected = select_national_sources(
        jurisdiction="FR",
        pathway_id="fr_passeport_talent",
        query="Passeport Talent categories",
    )
    assert selected
    assert selected[0]["id"] == "fr_passeport_talent"


def test_unsupported_jurisdiction_no_national_match():
    selected = select_national_sources(
        jurisdiction="AU",
        pathway_id=None,
        query="skilled migration",
    )
    assert selected == []


def test_allowlist_blocks_unapproved_host(monkeypatch):
    http = _FakeHttp({})
    conn = NationalOfficialConnector(http)

    # Inject a rogue catalog entry via monkeypatch of select results.
    rogue = [
        {
            "id": "rogue",
            "jurisdiction": "NL",
            "pathway_id": None,
            "title": "Rogue",
            "url": "https://evil.example/page",
            "hosts": ["evil.example"],
            "keywords": ["salary"],
        }
    ]
    monkeypatch.setattr(
        "lifepilot.external.providers.national_official.select_national_sources",
        lambda **kwargs: rogue,
    )
    out = conn.search(ExternalQuery(query="salary", jurisdiction="NL"))
    assert out.status == RetrievalStatus.BLOCKED or out.items == []
    assert http.calls == [] or all(
        "evil.example" not in c or True for c in http.calls
    )
    # Host check before fetch: should not call get_text for blocked host.
    assert http.calls == []


def test_redirect_off_allowlist_rejected():
    url = "https://ind.nl/en/residence-permits/work/highly-skilled-migrant"
    http = _FakeHttp(
        {
            url: HttpResponse(
                ok=True,
                status_code=200,
                url=url,
                final_url="https://phishing.example/ind",
                text="<html><p>Salary criterion information for highly skilled migrants.</p></html>",
                retrieval_status=RetrievalStatus.OK,
            )
        }
    )
    conn = NationalOfficialConnector(http)
    out = conn.search(
        ExternalQuery(
            query="highly skilled migrant salary",
            jurisdiction="NL",
            pathway_id="nl_highly_skilled_migrant",
        )
    )
    assert out.items == []
    assert out.status == RetrievalStatus.BLOCKED


def test_national_fetch_returns_unverified_passages():
    url = "https://www.mom.gov.sg/passes-and-permits/employment-pass"
    url2 = "https://www.mom.gov.sg/passes-and-permits/employment-pass/eligibility"
    html = (
        "<html><body><h1>Employment Pass</h1>"
        "<p>The Employment Pass allows foreign professionals to work in Singapore. "
        "Candidates must meet eligibility and qualifying salary requirements.</p>"
        "</body></html>"
    )
    ok = HttpResponse(
        ok=True,
        status_code=200,
        url=url,
        final_url=url,
        text=html,
        retrieval_status=RetrievalStatus.OK,
    )
    http = _FakeHttp({url: ok, url2: ok})
    # Also stub eligibility page miss so primary EP page is used.
    conn = NationalOfficialConnector(http)
    out = conn.search(
        ExternalQuery(
            query="Employment Pass salary eligibility",
            jurisdiction="SG",
            pathway_id="sg_employment_pass",
        )
    )
    assert out.status == RetrievalStatus.OK
    assert out.items
    assert out.items[0].verification_state.value == "unverified_external"
    assert out.items[0].jurisdiction == "SG"
    assert any("Employment Pass" in p or "salary" in p.lower() for p in out.items[0].passages)
