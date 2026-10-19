"""Tests for the hardened official seed source registry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete, func, select

from lifepilot.db.models import (
    Authority,
    Document,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.registry.importer import (
    import_seed_sources,
    resolve_patterns_for_import,
    sync_report_review_statuses,
)
from lifepilot.registry.review import ReviewError, set_source_review_status
from lifepilot.registry.seeds import SOURCES
from lifepilot.registry.urls import (
    InvalidRegistryUrlError,
    normalize_url,
    path_prefix_pattern,
    patterns_expand_scope,
    url_matches_allowed_pattern,
    validate_http_url,
    validate_seed_and_patterns,
)
from lifepilot.registry.verify import DEFAULT_USER_AGENT, VerificationResult, verify_seed_url


def _clear_registry(session) -> None:
    session.execute(delete(DocumentVersion))
    session.execute(delete(Document))
    session.execute(delete(Source))
    session.execute(delete(Authority))
    session.execute(delete(Jurisdiction))
    session.flush()


def _mock_client_head(final_url: str, status_code: int = 200) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.status_code = status_code
    response.url = final_url
    client.head.return_value = response
    return client


def test_normalize_url_dedupes_trivial_variants():
    a = normalize_url("https://www.gov.uk/skilled-worker-visa/")
    b = normalize_url("HTTPS://WWW.GOV.UK/skilled-worker-visa#section")
    assert a == b == "https://www.gov.uk/skilled-worker-visa"


def test_path_prefix_pattern_is_narrow():
    pattern = path_prefix_pattern("https://ind.nl/en/residence-permits/work")
    assert pattern == "https://ind.nl/en/residence-permits/work*"
    assert "ind.nl/*" not in pattern


def test_url_scope_allows_exact_and_descendant_paths():
    pattern = "https://ind.nl/en/residence-permits/work*"
    assert url_matches_allowed_pattern(
        "https://ind.nl/en/residence-permits/work", pattern
    )
    assert url_matches_allowed_pattern(
        "https://ind.nl/en/residence-permits/work/highly-skilled-migrant", pattern
    )


def test_url_scope_rejects_path_boundary_bypass_attempts():
    pattern = "https://ind.nl/en/residence-permits/work*"
    assert not url_matches_allowed_pattern(
        "https://ind.nl/en/residence-permits/work-evil", pattern
    )
    assert not url_matches_allowed_pattern(
        "https://ind.nl/en/residence-permits/worker", pattern
    )
    assert not url_matches_allowed_pattern(
        "https://ind.nl/en/residence-permits/workevil/page", pattern
    )


def test_url_scope_requires_exact_scheme_and_host():
    pattern = "https://www.gov.uk/skilled-worker-visa*"
    assert not url_matches_allowed_pattern(
        "http://www.gov.uk/skilled-worker-visa", pattern
    )
    assert not url_matches_allowed_pattern(
        "https://evil.example/skilled-worker-visa", pattern
    )
    assert not url_matches_allowed_pattern(
        "https://gov.uk/skilled-worker-visa", pattern
    )


def test_reject_embedded_credentials_and_unsupported_schemes():
    with pytest.raises(InvalidRegistryUrlError):
        normalize_url("https://user:pass@www.gov.uk/skilled-worker-visa")
    with pytest.raises(InvalidRegistryUrlError):
        validate_http_url("ftp://www.gov.uk/file")
    with pytest.raises(InvalidRegistryUrlError):
        validate_http_url("javascript:alert(1)")


def test_approved_seeds_require_https_patterns():
    with pytest.raises(InvalidRegistryUrlError):
        validate_seed_and_patterns(
            "http://www.gov.uk/skilled-worker-visa",
            ["http://www.gov.uk/skilled-worker-visa*"],
            require_https=True,
        )


def test_out_of_scope_pattern_rejected():
    with pytest.raises(InvalidRegistryUrlError):
        validate_seed_and_patterns(
            "https://www.gov.uk/skilled-worker-visa",
            ["https://www.gov.uk/*"],
            require_https=True,
        )


def test_import_is_idempotent(db_session):
    _clear_registry(db_session)
    first = import_seed_sources(db_session, verify=False)
    db_session.flush()
    second = import_seed_sources(db_session, verify=False)
    db_session.flush()

    source_count = db_session.scalar(select(func.count()).select_from(Source))
    assert source_count == len(SOURCES)
    assert first.sources_created == len(SOURCES)
    assert second.sources_created == 0
    assert second.sources_unchanged == len(SOURCES)


def test_import_keeps_original_seed_url_and_normalized(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()

    row = db_session.scalar(
        select(Source).where(
            Source.seed_url_normalized == "https://www.gov.uk/skilled-worker-visa"
        )
    )
    assert row is not None
    assert row.seed_url == "https://www.gov.uk/skilled-worker-visa"
    assert row.allowed_url_patterns == ["https://www.gov.uk/skilled-worker-visa*"]
    assert row.default_language == "en-GB"
    assert "topics=immigration, employment" in (row.notes or "")
    assert "crawl_permission=unchecked" in (row.notes or "")


def test_new_sources_are_candidates_even_without_verify(db_session):
    _clear_registry(db_session)
    report = import_seed_sources(db_session, verify=False)
    db_session.flush()
    assert report.approved_count == 0
    assert report.candidate_count == len(SOURCES)
    assert set(db_session.scalars(select(Source.review_status)).all()) == {"candidate"}


def test_candidate_remains_candidate_after_successful_http_verification(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()

    client = _mock_client_head("https://www.gov.uk/skilled-worker-visa")
    # Make each HEAD return the requested URL as final URL.
    def _head(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.url = url
        return resp

    client.head.side_effect = _head
    report = import_seed_sources(db_session, verify=True, http_client=client)
    db_session.flush()

    assert report.candidate_count == len(SOURCES)
    assert report.approved_count == 0
    assert all(item.verification and item.verification.ok for item in report.sources)
    statuses = set(db_session.scalars(select(Source.review_status)).all())
    assert statuses == {"candidate"}


def test_approved_remains_approved_after_transient_http_failure(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(select(Source).limit(1))
    assert source is not None
    set_source_review_status(db_session, source_id=source.id, status="approved")
    db_session.flush()

    import httpx

    client = MagicMock()
    client.head.side_effect = httpx.ConnectError("boom")
    stream_cm = MagicMock()
    stream_cm.__enter__.side_effect = httpx.ConnectError("boom")
    client.stream.return_value = stream_cm

    report = import_seed_sources(db_session, verify=True, http_client=client)
    db_session.flush()
    db_session.refresh(source)
    assert source.review_status == "approved"
    matching = [s for s in report.sources if s.seed_url_normalized == source.seed_url_normalized]
    assert matching[0].review_status == "approved"


def test_rejected_not_silently_reapproved(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(select(Source).limit(1))
    assert source is not None
    set_source_review_status(db_session, source_id=source.id, status="rejected")
    db_session.flush()

    client = _mock_client_head(source.seed_url)
    client.head.side_effect = lambda url, **kwargs: MagicMock(
        status_code=200, url=url
    )
    import_seed_sources(db_session, verify=True, http_client=client)
    db_session.flush()
    db_session.refresh(source)
    assert source.review_status == "rejected"


def test_existing_approved_seed_records_remain_intact(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(
        select(Source).where(
            Source.seed_url_normalized == "https://www.gov.uk/skilled-worker-visa"
        )
    )
    assert source is not None
    set_source_review_status(db_session, source_id=source.id, status="approved")
    original_id = source.id
    original_patterns = list(source.allowed_url_patterns)
    db_session.flush()

    report = import_seed_sources(db_session, verify=False)
    db_session.flush()
    db_session.refresh(source)

    assert source.id == original_id
    assert source.review_status == "approved"
    assert list(source.allowed_url_patterns) == original_patterns
    assert report.sources_created == 0
    gb = next(s for s in report.sources if s.key == "gb_skilled_worker_visa")
    assert gb.review_status == "approved"
    assert gb.action == "unchanged"


def test_cross_host_redirect_is_not_expected_host():
    client = _mock_client_head("https://validate.perfdrive.com/challenge")
    result = verify_seed_url(
        "https://www.make-it-in-germany.com/en/visa-residence/types",
        ("www.make-it-in-germany.com", "make-it-in-germany.com"),
        client=client,
    )
    assert isinstance(result, VerificationResult)
    assert result.connectivity_ok is True
    assert result.landed_on_expected_host is False
    assert result.ok is False
    assert result.redirected is True


def test_verifier_uses_transparent_lifepilot_user_agent():
    assert "LifePilotSeedVerifier" in DEFAULT_USER_AGENT
    assert "Mozilla" not in DEFAULT_USER_AGENT
    assert "Chrome" not in DEFAULT_USER_AGENT


def test_explicit_approval_requires_https_and_sets_status(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(select(Source).limit(1))
    assert source is not None
    set_source_review_status(db_session, source_id=source.id, status="approved")
    db_session.flush()
    db_session.refresh(source)
    assert source.review_status == "approved"


def test_explicit_approval_rejects_invalid_status(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(select(Source).limit(1))
    assert source is not None
    with pytest.raises(ReviewError):
        set_source_review_status(db_session, source_id=source.id, status="maybe")


def test_import_does_not_create_documents(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Document)) == 0


def test_eu_is_supranational(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    eu = db_session.scalar(select(Jurisdiction).where(Jurisdiction.code == "EU"))
    assert eu is not None
    assert eu.jurisdiction_type == "supranational"
    assert eu.country_code is None


def test_verify_seed_url_accepts_expected_host():
    client = _mock_client_head("https://ind.nl/en/residence-permits/work")
    result = verify_seed_url(
        "https://ind.nl/en/residence-permits/work",
        ("ind.nl", "www.ind.nl"),
        client=client,
    )
    assert result.ok is True
    assert result.landed_on_expected_host is True


def test_url_scope_compares_effective_ports():
    pattern = "https://www.gov.uk:443/skilled-worker-visa*"
    assert url_matches_allowed_pattern(
        "https://www.gov.uk/skilled-worker-visa", pattern
    )
    assert not url_matches_allowed_pattern(
        "https://www.gov.uk:8443/skilled-worker-visa", pattern
    )


def test_reject_encoded_separators_traversal_and_ambiguous_paths():
    with pytest.raises(InvalidRegistryUrlError):
        normalize_url("https://www.gov.uk/skilled%2Fworker-visa")
    with pytest.raises(InvalidRegistryUrlError):
        normalize_url("https://www.gov.uk/a/../b")
    with pytest.raises(InvalidRegistryUrlError):
        normalize_url("https://www.gov.uk/a//b")
    with pytest.raises(InvalidRegistryUrlError):
        normalize_url("https://www.gov.uk/a\\b")
    assert not url_matches_allowed_pattern(
        "https://www.gov.uk/skilled%2Fworker-visa",
        "https://www.gov.uk/skilled-worker-visa*",
    )


def test_patterns_expand_scope_detects_widening():
    existing = ["https://ind.nl/en/residence-permits/work*"]
    assert patterns_expand_scope(
        existing,
        ["https://ind.nl/en/residence-permits/work*", "https://ind.nl/en/*"],
    )
    assert patterns_expand_scope(existing, ["https://ind.nl/en/residence-permits/*"])
    assert not patterns_expand_scope(
        existing,
        ["https://ind.nl/en/residence-permits/work/highly-skilled-migrant*"],
    )


def test_approved_import_refuses_silent_scope_expansion(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(
        select(Source).where(
            Source.seed_url_normalized == "https://www.gov.uk/skilled-worker-visa"
        )
    )
    assert source is not None
    set_source_review_status(db_session, source_id=source.id, status="approved")
    original = list(source.allowed_url_patterns)
    db_session.flush()

    expanded = ["https://www.gov.uk/*"]
    stored, warning = resolve_patterns_for_import(
        review_status="approved",
        existing_patterns=original,
        proposed_patterns=expanded,
    )
    assert stored == original
    assert warning is not None
    assert "expand" in warning

    # Candidate sources may still take proposed patterns.
    candidate_stored, candidate_warning = resolve_patterns_for_import(
        review_status="candidate",
        existing_patterns=original,
        proposed_patterns=expanded,
    )
    assert candidate_stored == expanded
    assert candidate_warning is None


def test_approved_source_patterns_unchanged_across_reimport(db_session):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(
        select(Source).where(
            Source.seed_url_normalized
            == "https://ind.nl/en/residence-permits/work"
        )
    )
    assert source is not None
    # Simulate a previously reviewed narrow scope that differs from a wider proposal.
    source.allowed_url_patterns = [
        "https://ind.nl/en/residence-permits/work/highly-skilled-migrant*"
    ]
    set_source_review_status(db_session, source_id=source.id, status="approved")
    db_session.flush()
    protected = list(source.allowed_url_patterns)

    report = import_seed_sources(db_session, verify=False)
    db_session.flush()
    db_session.refresh(source)

    assert list(source.allowed_url_patterns) == protected
    nl = next(s for s in report.sources if s.key == "nl_ind_work_permits")
    assert nl.review_status == "approved"
    assert nl.warning is not None
    assert "scope" in nl.warning


def test_report_totals_reflect_explicit_review_ops(db_session):
    _clear_registry(db_session)
    report = import_seed_sources(db_session, verify=False)
    db_session.flush()
    assert report.approved_count == 0
    assert report.candidate_count == len(SOURCES)

    source = db_session.scalar(
        select(Source).where(
            Source.seed_url_normalized == "https://www.gov.uk/skilled-worker-visa"
        )
    )
    assert source is not None
    set_source_review_status(db_session, source_id=source.id, status="approved")
    db_session.flush()

    sync_report_review_statuses(db_session, report)
    assert report.approved_count == 1
    assert report.candidate_count == len(SOURCES) - 1
    gb = next(s for s in report.sources if s.key == "gb_skilled_worker_visa")
    assert gb.review_status == "approved"
