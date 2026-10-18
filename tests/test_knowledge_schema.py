"""Focused tests for knowledge-collection constraints and relationships.

Uses clearly synthetic fixture data only (not real government records).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from lifepilot.db.current_version import (
    content_matches_current,
    get_current_version,
    set_current_version,
)
from lifepilot.db.models import (
    Authority,
    Document,
    DocumentJurisdiction,
    DocumentVersion,
    Jurisdiction,
    Source,
)


def _sha(char: str) -> str:
    return (char * 64)[:64]


def _seed_graph(session):
    """Minimal synthetic graph for constraint tests."""
    country = Jurisdiction(
        code="XX-TEST",
        name="Testland",
        jurisdiction_type="country",
        country_code="XX",
    )
    region = Jurisdiction(
        code="XX-TEST-R1",
        name="Test Region",
        jurisdiction_type="region",
        country_code="XX",
        parent=country,
    )
    session.add_all([country, region])
    session.flush()

    authority = Authority(
        jurisdiction_id=country.id,
        name="Test Authority",
        short_name="TA",
    )
    session.add(authority)
    session.flush()

    source = Source(
        authority_id=authority.id,
        jurisdiction_id=country.id,
        name="Test Source",
        seed_url="https://example.test/",
        seed_url_normalized="https://example.test/",
        review_status="approved",
        default_language="en",
    )
    session.add(source)
    session.flush()
    return country, region, authority, source


def _make_document(session, source, *, url: str, kind: str = "html_page") -> Document:
    doc = Document(
        authority_id=source.authority_id,
        primary_source_id=source.id,
        canonical_url=url,
        url_normalized=url,
        content_kind=kind,
        language="en",
        discovery_status="discovered",
    )
    session.add(doc)
    session.flush()
    return doc


def test_url_normalized_is_unique(db_session):
    _, _, _, source = _seed_graph(db_session)
    _make_document(db_session, source, url="https://example.test/a")
    db_session.add(
        Document(
            authority_id=source.authority_id,
            primary_source_id=source.id,
            canonical_url="https://example.test/a",
            url_normalized="https://example.test/a",
            content_kind="html_page",
            discovery_status="discovered",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_seed_url_normalized_is_unique(db_session):
    country, _, authority, _ = _seed_graph(db_session)
    db_session.add(
        Source(
            authority_id=authority.id,
            jurisdiction_id=country.id,
            name="Duplicate seed",
            seed_url="https://example.test/",
            seed_url_normalized="https://example.test/",
            review_status="candidate",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_content_kind_docx_allowed_and_invalid_rejected(db_session):
    _, _, _, source = _seed_graph(db_session)
    doc = _make_document(
        db_session, source, url="https://example.test/file.docx", kind="docx"
    )
    assert doc.content_kind == "docx"

    bad = Document(
        authority_id=source.authority_id,
        primary_source_id=source.id,
        canonical_url="https://example.test/bad",
        url_normalized="https://example.test/bad",
        content_kind="rtf",
        discovery_status="discovered",
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_sha_can_reappear_as_new_version(db_session):
    """No unique(document_id, content_sha256); historical reverts are allowed."""
    _, _, _, source = _seed_graph(db_session)
    doc = _make_document(db_session, source, url="https://example.test/page")
    sha = _sha("a")
    now = datetime.now(timezone.utc)

    v1 = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=source.id,
        version_number=1,
        source_url=doc.canonical_url,
        retrieved_at=now,
        content_sha256=sha,
        storage_path="data/raw/test/v1.html",
        is_current=False,
    )
    v2 = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=source.id,
        version_number=2,
        source_url=doc.canonical_url,
        retrieved_at=now,
        content_sha256=_sha("b"),
        storage_path="data/raw/test/v2.html",
        is_current=True,
    )
    v3 = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=source.id,
        version_number=3,
        source_url=doc.canonical_url,
        retrieved_at=now,
        content_sha256=sha,  # same as v1
        storage_path="data/raw/test/v3.html",
        is_current=False,
    )
    db_session.add_all([v1, v2, v3])
    db_session.flush()
    assert content_matches_current(db_session, doc.id, _sha("b"))
    assert not content_matches_current(db_session, doc.id, sha)


def test_at_most_one_current_version_partial_unique(db_session):
    _, _, _, source = _seed_graph(db_session)
    doc = _make_document(db_session, source, url="https://example.test/current")
    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            DocumentVersion(
                document_id=doc.id,
                version_number=1,
                source_url=doc.canonical_url,
                retrieved_at=now,
                content_sha256=_sha("1"),
                storage_path="data/raw/test/c1.html",
                is_current=True,
            ),
            DocumentVersion(
                document_id=doc.id,
                version_number=2,
                source_url=doc.canonical_url,
                retrieved_at=now,
                content_sha256=_sha("2"),
                storage_path="data/raw/test/c2.html",
                is_current=True,
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_set_current_version_is_transactional(db_session):
    _, _, _, source = _seed_graph(db_session)
    doc = _make_document(db_session, source, url="https://example.test/switch")
    now = datetime.now(timezone.utc)
    v1 = DocumentVersion(
        document_id=doc.id,
        version_number=1,
        source_url=doc.canonical_url,
        retrieved_at=now,
        content_sha256=_sha("1"),
        storage_path="data/raw/test/s1.html",
        is_current=True,
    )
    v2 = DocumentVersion(
        document_id=doc.id,
        version_number=2,
        source_url=doc.canonical_url,
        retrieved_at=now,
        content_sha256=_sha("2"),
        storage_path="data/raw/test/s2.html",
        is_current=False,
    )
    db_session.add_all([v1, v2])
    db_session.flush()

    set_current_version(db_session, v2)
    db_session.flush()

    current = get_current_version(db_session, doc.id)
    assert current is not None
    assert current.id == v2.id
    db_session.refresh(v1)
    assert v1.is_current is False


def test_document_jurisdictions_explicit_only(db_session):
    """Linking is explicit; creating a doc under an authority does not auto-tag."""
    country, region, _, source = _seed_graph(db_session)
    doc = _make_document(db_session, source, url="https://example.test/policy")
    db_session.flush()
    assert doc.jurisdiction_links == []

    db_session.add(
        DocumentJurisdiction(
            document_id=doc.id,
            jurisdiction_id=country.id,
            relevance="applicable",
        )
    )
    db_session.add(
        DocumentJurisdiction(
            document_id=doc.id,
            jurisdiction_id=region.id,
            relevance="related",
        )
    )
    db_session.flush()
    db_session.refresh(doc)
    assert len(doc.jurisdiction_links) == 2


def test_version_number_unique_per_document(db_session):
    _, _, _, source = _seed_graph(db_session)
    doc = _make_document(db_session, source, url="https://example.test/ver")
    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            DocumentVersion(
                document_id=doc.id,
                version_number=1,
                source_url=doc.canonical_url,
                retrieved_at=now,
                content_sha256=_sha("x"),
                storage_path="data/raw/test/x1.html",
                is_current=True,
            ),
            DocumentVersion(
                document_id=doc.id,
                version_number=1,
                source_url=doc.canonical_url,
                retrieved_at=now,
                content_sha256=_sha("y"),
                storage_path="data/raw/test/x2.html",
                is_current=False,
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
