"""Phase 6C.2 registry config import, country selection, and onboarding tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import delete, func, select

from lifepilot.db.models import (
    Authority,
    Document,
    DocumentContentComparison,
    DocumentExtraction,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.ingest.pipeline import IngestLimits, run_ingestion
from lifepilot.registry.config_loader import RegistryConfigError, load_registry_config
from lifepilot.registry.importer import import_registry_config, import_seed_sources
from lifepilot.registry.onboarding import deepen_source_verification
from lifepilot.registry.seeds import AuthoritySeed, JurisdictionSeed, SourceSeed
from lifepilot.registry.urls import path_prefix_pattern


def _clear(session) -> None:
    session.execute(delete(DocumentContentComparison))
    session.execute(delete(DocumentExtraction))
    session.execute(delete(DocumentVersion))
    session.execute(delete(Document))
    session.execute(delete(Source))
    session.execute(delete(Authority))
    session.execute(delete(Jurisdiction))
    session.flush()


def _sample_config(tmp_path: Path, *, bad_url: bool = False) -> Path:
    seed_url = (
        "http://example.com/not-https"
        if bad_url
        else "https://www.mom.gov.sg/passes-and-permits/employment-pass"
    )
    payload = {
        "jurisdictions": [
            {
                "code": "SG",
                "name": "Singapore",
                "jurisdiction_type": "country",
                "country_code": "SG",
            }
        ],
        "authorities": [
            {
                "key": "sg_mom",
                "jurisdiction_code": "SG",
                "name": "Ministry of Manpower (MOM)",
                "short_name": "MOM",
                "website_url": "https://www.mom.gov.sg/",
            }
        ],
        "sources": [
            {
                "key": "sg_mom_employment_pass",
                "jurisdiction_code": "SG",
                "authority_key": "sg_mom",
                "name": "MOM — Employment Pass",
                "seed_url": seed_url,
                "language": "en",
                "topics": ["immigration", "employment"],
                "expected_hosts": ["www.mom.gov.sg", "mom.gov.sg"],
                "source_type": "html_guidance",
            }
        ],
    }
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_config_and_idempotent_import(db_session, tmp_path: Path):
    _clear(db_session)
    path = _sample_config(tmp_path)
    cfg = load_registry_config(path)
    first = import_registry_config(
        db_session,
        jurisdictions=cfg.jurisdictions,
        authorities=cfg.authorities,
        sources=cfg.sources,
        verify=False,
    )
    db_session.flush()
    assert first.jurisdictions_created == 1
    assert first.authorities_created == 1
    assert first.sources_created == 1
    assert first.sources[0].review_status == "candidate"

    second = import_registry_config(
        db_session,
        jurisdictions=cfg.jurisdictions,
        authorities=cfg.authorities,
        sources=cfg.sources,
        verify=False,
    )
    db_session.flush()
    assert second.jurisdictions_created == 0
    assert second.sources_created == 0
    assert second.sources_unchanged == 1
    assert db_session.scalar(select(func.count()).select_from(Source)) == 1
    source = db_session.scalar(select(Source))
    assert source is not None
    assert source.review_status == "candidate"


def test_duplicate_jurisdiction_code_rejected(tmp_path: Path):
    payload = {
        "jurisdictions": [
            {
                "code": "SG",
                "name": "Singapore",
                "jurisdiction_type": "country",
                "country_code": "SG",
            },
            {
                "code": "SG",
                "name": "Singapore Dup",
                "jurisdiction_type": "country",
                "country_code": "SG",
            },
        ],
        "authorities": [
            {
                "key": "a",
                "jurisdiction_code": "SG",
                "name": "A",
            }
        ],
        "sources": [
            {
                "key": "s",
                "jurisdiction_code": "SG",
                "authority_key": "a",
                "name": "S",
                "seed_url": "https://www.mom.gov.sg/x",
                "language": "en",
                "topics": ["immigration"],
                "expected_hosts": ["www.mom.gov.sg"],
            }
        ],
    }
    path = tmp_path / "dup.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RegistryConfigError, match="duplicate jurisdiction"):
        load_registry_config(path)


def test_invalid_country_code_rejected(tmp_path: Path):
    payload = {
        "jurisdictions": [
            {
                "code": "SGP",
                "name": "Bad",
                "jurisdiction_type": "country",
                "country_code": "SGP",
            }
        ],
        "authorities": [{"key": "a", "jurisdiction_code": "SGP", "name": "A"}],
        "sources": [
            {
                "key": "s",
                "jurisdiction_code": "SGP",
                "authority_key": "a",
                "name": "S",
                "seed_url": "https://example.gov/x",
                "language": "en",
                "topics": ["immigration"],
                "expected_hosts": ["example.gov"],
            }
        ],
    }
    path = tmp_path / "badcc.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RegistryConfigError, match="ISO 3166-1"):
        load_registry_config(path)


def test_invalid_http_seed_skipped(db_session, tmp_path: Path):
    _clear(db_session)
    path = _sample_config(tmp_path, bad_url=True)
    cfg = load_registry_config(path)
    report = import_registry_config(
        db_session,
        jurisdictions=cfg.jurisdictions,
        authorities=cfg.authorities,
        sources=cfg.sources,
        verify=False,
    )
    assert report.sources[0].action == "skipped_invalid"
    assert db_session.scalar(select(func.count()).select_from(Source)) == 0


def test_no_automatic_approvals_on_config_import(db_session, tmp_path: Path):
    _clear(db_session)
    path = _sample_config(tmp_path)
    cfg = load_registry_config(path)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, request=request)
    )
    client = httpx.Client(transport=transport, follow_redirects=True)
    report = import_registry_config(
        db_session,
        jurisdictions=cfg.jurisdictions,
        authorities=cfg.authorities,
        sources=cfg.sources,
        verify=True,
        http_client=client,
    )
    assert report.sources_created == 1
    assert report.sources[0].review_status == "candidate"
    assert report.approved_count == 0


def test_country_selection_excludes_candidates_and_eu_expansion(db_session, tmp_path: Path):
    _clear(db_session)
    # Seed baseline curated sources without verify.
    import_seed_sources(db_session, verify=False)
    db_session.flush()

    # Approve one GB source only.
    gb = db_session.scalar(
        select(Source).where(Source.seed_url_normalized.contains("skilled-worker-visa"))
    )
    assert gb is not None
    gb.review_status = "approved"
    # Ensure EU source stays candidate.
    eu = db_session.scalar(
        select(Source).where(Source.seed_url_normalized.contains("youreurope"))
    )
    if eu is not None:
        eu.review_status = "candidate"
    db_session.flush()

    report = run_ingestion(
        db_session,
        execute=False,
        country="GB",
        all_approved=True,
        storage_root=tmp_path,
    )
    assert report.selected_sources
    assert all(s["review_status"] == "approved" for s in report.selected_sources)
    assert all(
        "gov.uk" in (s.get("seed_url") or "") for s in report.selected_sources
    )

    eu_report = run_ingestion(
        db_session,
        execute=False,
        country="FR",
        all_approved=True,
        storage_root=tmp_path,
    )
    # FR has no approved sources; EU must not leak into FR selection.
    assert eu_report.selected_sources == []
    assert any("no approved sources" in e for e in eu_report.exclusions)


def test_country_conflicts_with_source_id(db_session, tmp_path: Path):
    _clear(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(select(Source))
    assert source is not None
    source.review_status = "approved"
    db_session.flush()
    report = run_ingestion(
        db_session,
        execute=False,
        source_id=source.id,
        country="GB",
        storage_root=tmp_path,
    )
    assert report.selected_sources == []
    assert any("cannot combine" in e for e in report.exclusions)


def test_candidate_never_selected_for_country_execute(db_session, tmp_path: Path):
    _clear(db_session)
    cfg = load_registry_config(_sample_config(tmp_path))
    import_registry_config(
        db_session,
        jurisdictions=cfg.jurisdictions,
        authorities=cfg.authorities,
        sources=cfg.sources,
        verify=False,
    )
    db_session.flush()
    report = run_ingestion(
        db_session,
        execute=True,
        country="SG",
        all_approved=True,
        limits=IngestLimits(max_fetches=1, max_extractions=0, max_comparisons=0),
        storage_root=tmp_path,
        write_report_file=False,
    )
    assert report.selected_sources == []
    assert report.fetches_attempted == 0


def test_onboarding_records_blocked_site(tmp_path: Path):
    seed = SourceSeed(
        key="blocked",
        jurisdiction_code="DE",
        authority_key="x",
        name="Blocked",
        seed_url="https://www.example.gov/page",
        language="en",
        topics=("immigration",),
        expected_hosts=("www.example.gov", "example.gov"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        html = "<html><head><title>Radware Page</title></head><body>challenge</body></html>"
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    record = deepen_source_verification(
        seed,
        authority_name="Example",
        path_prefix_patterns=[path_prefix_pattern(seed.seed_url)],
        http_client=client,
    )
    assert record.facts.get("bot_protection_suspected") is True
    assert record.recommendation == "further_review"
    assert any("bot-protection" in w for w in record.warnings)


def test_global_limits_with_country_selection(db_session, tmp_path: Path):
    _clear(db_session)
    # Two SG candidate sources via direct seeds.
    jurisdictions = (
        JurisdictionSeed("SG", "Singapore", "country", "SG"),
    )
    authorities = (
        AuthoritySeed("sg_mom", "SG", "Ministry of Manpower (MOM)", "MOM"),
    )
    sources = (
        SourceSeed(
            key="sg_a",
            jurisdiction_code="SG",
            authority_key="sg_mom",
            name="SG A",
            seed_url="https://www.mom.gov.sg/passes-and-permits/employment-pass",
            language="en",
            topics=("immigration",),
            expected_hosts=("www.mom.gov.sg",),
        ),
        SourceSeed(
            key="sg_b",
            jurisdiction_code="SG",
            authority_key="sg_mom",
            name="SG B",
            seed_url="https://www.mom.gov.sg/passes-and-permits/s-pass",
            language="en",
            topics=("immigration",),
            expected_hosts=("www.mom.gov.sg",),
        ),
    )
    import_registry_config(
        db_session,
        jurisdictions=jurisdictions,
        authorities=authorities,
        sources=sources,
        verify=False,
    )
    db_session.flush()
    for src in db_session.scalars(select(Source)).all():
        src.review_status = "approved"
    db_session.flush()

    report = run_ingestion(
        db_session,
        execute=False,
        country="SG",
        limits=IngestLimits(max_sources=1),
        storage_root=tmp_path,
    )
    assert len(report.selected_sources) == 1
    assert "max_sources" in report.limits_reached
