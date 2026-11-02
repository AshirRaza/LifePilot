"""Phase 6D.8: PageSchema link discovery + exact allowlist PDF seeding."""

from __future__ import annotations

import html as html_mod
import json

from sqlalchemy import delete, select

from lifepilot.crawl.allowlist_exact import (
    enqueue_exact_allowlist_urls,
    iter_exact_allowlist_urls,
)
from lifepilot.crawl.content_language import infer_text_language
from lifepilot.crawl.frontier import enqueue_seed
from lifepilot.crawl.links import discover_links_from_html
from lifepilot.crawl.pageschema_links import (
    discover_links_from_pageschema,
    extract_raw_pageschema_hrefs,
)
from lifepilot.db.models import (
    Authority,
    DiscoveryFrontier,
    Document,
    DocumentContentComparison,
    DocumentExtraction,
    DocumentVersion,
    Jurisdiction,
    Source,
)


def _clear(session) -> None:
    session.execute(delete(DocumentContentComparison))
    session.execute(delete(DocumentExtraction))
    session.execute(delete(DiscoveryFrontier))
    session.execute(delete(DocumentVersion))
    session.execute(delete(Document))
    session.execute(delete(Source))
    session.execute(delete(Authority))
    session.execute(delete(Jurisdiction))
    session.flush()


def _source(session, *, patterns: list[str], seed: str, name: str = "src") -> Source:
    country = Jurisdiction(
        code=f"XX-{name[:6]}",
        name="Example",
        jurisdiction_type="country",
        country_code="XX",
    )
    session.add(country)
    session.flush()
    authority = Authority(jurisdiction_id=country.id, name=f"Auth {name}")
    session.add(authority)
    session.flush()
    source = Source(
        authority_id=authority.id,
        jurisdiction_id=country.id,
        name=name,
        seed_url=seed,
        seed_url_normalized=seed,
        allowed_url_patterns=patterns,
        review_status="approved",
    )
    session.add(source)
    session.flush()
    return source


def _pageschema_html(payload: dict) -> bytes:
    escaped = html_mod.escape(json.dumps(payload), quote=True)
    return f"""<!DOCTYPE html>
<html><head><title>Hub</title></head>
<body>
<main id="content"></main>
<input type="hidden" id="ctl00_PlaceHolderMain_PageSchemaHiddenField_Input"
       value="{escaped}" />
<script type="application/json" id="siteData">{{
  "menuItems": [{{"title": "Visas", "url": "/visas/elsewhere"}}],
  "languagePickerItems": []
}}</script>
</body></html>""".encode()


def test_pageschema_extracts_tile_links_and_relative_resolution():
    html = _pageschema_html(
        {
            "tiles": {
                "tiles": [
                    {
                        "title": "Regional",
                        "link": "/visas/working-in-australia/regional-migration",
                    },
                    {
                        "title": "Temp",
                        "link": "/visas/working-in-australia/temporary-work-visas",
                    },
                    {
                        "title": "Other host",
                        "link": "https://example.org/outside",
                    },
                    {"title": "Dup", "link": "/visas/working-in-australia/regional-migration"},
                    {"title": "Opaque", "link": "482-69"},
                    {"title": "Hash", "link": "#"},
                ]
            }
        }
    )
    base = "https://immi.homeaffairs.gov.au/visas/working-in-australia"
    patterns = ["https://immi.homeaffairs.gov.au/visas/working-in-australia*"]
    raw = extract_raw_pageschema_hrefs(html)
    assert "/visas/working-in-australia/regional-migration" in raw
    assert raw.count("/visas/working-in-australia/regional-migration") == 1
    assert "482-69" not in raw
    assert "#" not in raw
    # siteData menu must not contribute
    assert "/visas/elsewhere" not in raw

    links = discover_links_from_pageschema(
        html, base_url=base, allowed_patterns=patterns
    )
    in_scope = [L for L in links if L.kind.startswith("in_scope")]
    oos = [L for L in links if L.kind == "out_of_scope"]
    assert {
        L.url_normalized
        for L in in_scope
    } == {
        "https://immi.homeaffairs.gov.au/visas/working-in-australia/regional-migration",
        "https://immi.homeaffairs.gov.au/visas/working-in-australia/temporary-work-visas",
    }
    assert any("example.org/outside" in (L.url_normalized or "") for L in oos)


def test_pageschema_hrefs_inside_html_fragments():
    html = _pageschema_html(
        {
            "description": (
                '<p>See <a href="/citizenship/become-a-citizen/conferral">conferral</a></p>'
            ),
            "warning": '<p><a href="mailto:x@example.com">mail</a></p>',
        }
    )
    base = "https://immi.homeaffairs.gov.au/citizenship/become-a-citizen"
    patterns = ["https://immi.homeaffairs.gov.au/citizenship/become-a-citizen*"]
    links = discover_links_from_pageschema(
        html, base_url=base, allowed_patterns=patterns
    )
    assert any(
        L.kind == "in_scope_html"
        and L.url_normalized
        == "https://immi.homeaffairs.gov.au/citizenship/become-a-citizen/conferral"
        for L in links
    )
    assert all(L.kind != "in_scope_html" or "mailto" not in L.raw_href for L in links)


def test_pageschema_malformed_json_ignored():
    html = b"""<html><body><main></main>
    <input type="hidden" value="{not-json" />
    <input type="hidden" value="just text" />
    </body></html>"""
    assert extract_raw_pageschema_hrefs(html) == []
    links = discover_links_from_pageschema(
        html,
        base_url="https://example.test/page",
        allowed_patterns=["https://example.test/page*"],
    )
    assert links == []


def test_anchor_discovery_unchanged_when_no_pageschema():
    html = b"""<html><body><main>
    <a href="/guidance/visa/eligibility">Eligibility</a>
    <a href="/elsewhere">Out</a>
    </main></body></html>"""
    links = discover_links_from_html(
        html.decode(),
        base_url="https://example.test/guidance/visa",
        allowed_patterns=["https://example.test/guidance/visa*"],
    )
    assert any(
        L.kind == "in_scope_html"
        and L.url_normalized == "https://example.test/guidance/visa/eligibility"
        for L in links
    )
    assert any(L.kind == "out_of_scope" for L in links)


def test_exact_allowlist_urls_exclude_wildcards():
    items = iter_exact_allowlist_urls(
        [
            "https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation*",
            "https://ind.nl/en/forms/5013.pdf",
            "https://ind.nl/en/forms/6075.pdf",
            "https://ind.nl/en/forms/*",
        ]
    )
    urls = [i.url_normalized for i in items]
    assert urls == [
        "https://ind.nl/en/forms/5013.pdf",
        "https://ind.nl/en/forms/6075.pdf",
    ]
    assert all(i.content_kind_hint == "pdf" for i in items)


def test_enqueue_exact_allowlist_urls_idempotent(db_session):
    _clear(db_session)
    source = _source(
        db_session,
        name="ind",
        seed="https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation",
        patterns=[
            "https://ind.nl/en/dutch-citizenship/becoming-a-dutch-national-through-naturalisation*",
            "https://ind.nl/en/forms/5013.pdf",
            "https://ind.nl/en/forms/6075.pdf",
        ],
    )
    enqueue_seed(db_session, source)
    first = enqueue_exact_allowlist_urls(db_session, source)
    assert sum(1 for r in first if r.outcome == "inserted_pending") == 2
    second = enqueue_exact_allowlist_urls(db_session, source)
    assert all(r.outcome == "unchanged" for r in second)
    rows = list(
        db_session.scalars(
            select(DiscoveryFrontier).where(DiscoveryFrontier.source_id == source.id)
        ).all()
    )
    methods = {r.url_normalized: r.discovery_method for r in rows}
    assert (
        methods["https://ind.nl/en/forms/5013.pdf"] == "allowlist_exact"
    )
    assert (
        methods["https://ind.nl/en/forms/6075.pdf"] == "allowlist_exact"
    )
    assert all(r.status == "pending" for r in rows if r.discovery_method == "allowlist_exact")


def test_opaque_482_pageschema_has_no_usable_urls():
    html = _pageschema_html(
        {
            "recommended": {
                "cards": [
                    {"id": "482-69", "itemName": "482-69"},
                    {"id": "482-70", "itemName": "482-70"},
                ]
            },
            "title": "Skills in Demand visa",
            "description": "<p>Temporary sponsored work visa.</p>",
            "eligibility": "<p>You must have an approved sponsor.</p>",
            "warning": 'Warning text here. <a href="#">Anchor</a>',
        }
    )
    raw = extract_raw_pageschema_hrefs(html)
    assert raw == []
    links = discover_links_from_pageschema(
        html,
        base_url=(
            "https://immi.homeaffairs.gov.au/visas/getting-a-visa/"
            "visa-listing/skills-in-demand-visa-subclass-482"
        ),
        allowed_patterns=[
            "https://immi.homeaffairs.gov.au/visas/getting-a-visa/"
            "visa-listing/skills-in-demand-visa-subclass-482*"
        ],
    )
    assert links == []


def test_infer_text_language_prefers_content_not_path_words():
    dutch = (
        "U wordt Nederlander. Wat mag en moet u doen? "
        "U gaat naar de naturalisatieceremonie om de bekendmaking "
        "van verlening van het Nederlanderschap in ontvangst te nemen."
    )
    english = (
        "You must meet the requirements for the residence permit "
        "and submit your application with the required documents."
    )
    assert infer_text_language(dutch) == "nl"
    assert infer_text_language(english) == "en"
