"""Sitemap parse + allowlist-bound enqueue tests."""

from __future__ import annotations

import gzip

from sqlalchemy import delete, select

from lifepilot.crawl.frontier import enqueue_url
from lifepilot.crawl.robots import parse_sitemap_directives
from lifepilot.crawl.sitemap import parse_sitemap_xml
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


def test_parse_sitemap_directives_from_robots():
    body = """
User-agent: *
Disallow: /search
Sitemap: https://www.example.test/sitemap.xml
Sitemap: https://www.example.test/sitemap-news.xml
# Sitemap: https://ignored.test/x.xml
Sitemap: https://www.example.test/sitemap.xml
"""
    urls = parse_sitemap_directives(body)
    assert urls == [
        "https://www.example.test/sitemap.xml",
        "https://www.example.test/sitemap-news.xml",
    ]


def test_parse_urlset_and_index():
    urlset = b"""<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://www.example.test/guidance/visa</loc></url>
      <url><loc>https://www.example.test/guidance/visa/eligibility</loc></url>
    </urlset>
    """
    kind, locs = parse_sitemap_xml(urlset)
    assert kind == "urlset"
    assert locs == [
        "https://www.example.test/guidance/visa",
        "https://www.example.test/guidance/visa/eligibility",
    ]

    index = b"""<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://www.example.test/s1.xml</loc></sitemap>
    </sitemapindex>
    """
    kind2, locs2 = parse_sitemap_xml(index)
    assert kind2 == "sitemapindex"
    assert locs2 == ["https://www.example.test/s1.xml"]


def test_parse_gzip_sitemap():
    raw = b"""<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://www.example.test/a</loc></url>
    </urlset>
    """
    compressed = gzip.compress(raw)
    kind, locs = parse_sitemap_xml(compressed)
    assert kind == "urlset"
    assert locs == ["https://www.example.test/a"]


def test_sitemap_locs_respect_allowlist(db_session):
    _clear(db_session)
    country = Jurisdiction(
        code="XX-SM",
        name="Example",
        jurisdiction_type="country",
        country_code="XX",
    )
    db_session.add(country)
    db_session.flush()
    authority = Authority(jurisdiction_id=country.id, name="Auth")
    db_session.add(authority)
    db_session.flush()
    seed = "https://www.example.test/guidance/visa"
    source = Source(
        authority_id=authority.id,
        jurisdiction_id=country.id,
        name="sm",
        seed_url=seed,
        seed_url_normalized=seed,
        allowed_url_patterns=[f"{seed}*"],
        review_status="approved",
    )
    db_session.add(source)
    db_session.flush()

    in_scope = enqueue_url(
        db_session,
        source,
        url="https://www.example.test/guidance/visa/eligibility",
        discovery_method="sitemap",
        depth=1,
        parent_url="https://www.example.test/sitemap.xml",
    )
    assert in_scope.outcome == "inserted_pending"
    out = enqueue_url(
        db_session,
        source,
        url="https://www.example.test/unrelated/page",
        discovery_method="sitemap",
        depth=1,
        parent_url="https://www.example.test/sitemap.xml",
    )
    assert out.outcome == "inserted_out_of_scope"
    rows = list(db_session.scalars(select(DiscoveryFrontier)).all())
    assert len(rows) == 2
