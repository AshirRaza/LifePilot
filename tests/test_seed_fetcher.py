"""Mocked HTTP tests for approved-seed HTML fetcher."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy import delete, func, select

from lifepilot.crawl.fetcher import (
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_FETCHED_NEW_VERSION,
    STATUS_SKIPPED,
    STATUS_UNCHANGED,
    fetch_approved_seed,
    fetch_approved_seeds,
)
from lifepilot.crawl.http_fetch import fetch_html_seed
from lifepilot.crawl.policy import FETCHER_USER_AGENT, fetcher_policy
from lifepilot.db.models import (
    Authority,
    Document,
    DocumentContentComparison,
    DocumentExtraction,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.registry.importer import import_seed_sources
from lifepilot.registry.review import set_source_review_status


def _clear_registry(session) -> None:
    session.execute(delete(DocumentContentComparison))
    session.execute(delete(DocumentExtraction))
    session.execute(delete(DocumentVersion))
    session.execute(delete(Document))
    session.execute(delete(Source))
    session.execute(delete(Authority))
    session.execute(delete(Jurisdiction))
    session.flush()


def _approve_one(session) -> Source:
    import_seed_sources(session, verify=False)
    session.flush()
    source = session.scalar(
        select(Source).where(
            Source.seed_url_normalized == "https://www.gov.uk/skilled-worker-visa"
        )
    )
    assert source is not None
    set_source_review_status(session, source_id=source.id, status="approved")
    session.flush()
    return source


def _stream_response(
    *,
    status_code: int = 200,
    headers: dict | None = None,
    body: bytes = b"<html><body>ok</body></html>",
):
    stream = MagicMock()
    stream.status_code = status_code
    stream.headers = headers or {"content-type": "text/html; charset=utf-8"}
    # New iterator per call so repeated fetches see the same body.
    stream.iter_bytes.side_effect = lambda: iter([body])
    stream.close = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = stream
    cm.__exit__.return_value = False
    return cm


def _mock_client(
    *,
    html: bytes = b"<html><body>hello</body></html>",
    content_type: str = "text/html; charset=utf-8",
    robots_text: str = "User-agent: *\nAllow: /\n",
    stream_side_effect=None,
) -> MagicMock:
    client = MagicMock()
    client.get.return_value = MagicMock(status_code=200, text=robots_text)
    if stream_side_effect is not None:
        client.stream.side_effect = stream_side_effect
    else:
        client.stream.return_value = _stream_response(
            headers={"content-type": content_type},
            body=html,
        )
    return client


def test_fetcher_user_agent_is_transparent():
    assert "LifePilotFetcher" in FETCHER_USER_AGENT
    assert "Mozilla" not in FETCHER_USER_AGENT
    assert "HTML/PDF/DOCX" in FETCHER_USER_AGENT


def test_content_kind_conflict_rejects_without_new_version(db_session, tmp_path: Path):
    """Reuse of url_normalized must fail when content_kind conflicts."""
    from lifepilot.crawl.fetcher import fetch_binary_document_url

    _clear_registry(db_session)
    source = _approve_one(db_session)
    html = b"<html><body>seed</body></html>"
    first = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=_mock_client(html=html),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    assert first.status == STATUS_FETCHED_NEW_VERSION
    assert first.document_id is not None

    # Force same normalized URL as the seed HTML document.
    doc = db_session.get(Document, first.document_id)
    assert doc is not None
    pdf_url = doc.url_normalized
    conflict = fetch_binary_document_url(
        db_session,
        source,
        url=pdf_url,
        expected_kind="pdf",
        persist=True,
        http_client=_mock_client(
            html=b"%PDF-1.4 conflict",
            content_type="application/pdf",
        ),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()

    assert conflict.status == STATUS_FAILED
    assert "content_kind" in conflict.message
    assert conflict.version_id is None
    assert db_session.scalar(select(func.count()).select_from(DocumentVersion)) == 1
    db_session.refresh(doc)
    assert doc.content_kind == "html_page"


def test_dry_run_does_not_persist(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_one(db_session)
    client = _mock_client()

    result = fetch_approved_seed(
        db_session,
        source.id,
        persist=False,
        policy=fetcher_policy(),
        http_client=client,
        storage_root=tmp_path,
    )
    db_session.flush()

    assert result.status == STATUS_DRY_RUN
    assert db_session.scalar(select(func.count()).select_from(Document)) == 0
    assert list(tmp_path.rglob("*.html")) == []
    # Dry-run preflight may check robots, but must not stream the page.
    client.stream.assert_not_called()


def test_persist_creates_document_and_version(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_one(db_session)
    html = b"<html><body>version-one</body></html>"
    client = _mock_client(html=html)

    result = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        policy=fetcher_policy(),
        http_client=client,
        storage_root=tmp_path,
    )
    db_session.flush()

    assert result.status == STATUS_FETCHED_NEW_VERSION
    assert result.document_id is not None
    assert result.version_number == 1
    doc = db_session.get(Document, result.document_id)
    assert doc is not None
    assert doc.content_kind == "html_page"
    assert doc.discovery_status == "seed"
    version = db_session.get(DocumentVersion, result.version_id)
    assert version is not None
    assert version.is_current is True
    assert version.http_status == 200
    assert version.byte_size == len(html)
    stored = tmp_path / result.storage_path
    assert stored.read_bytes() == html


def test_unchanged_content_does_not_create_new_version(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_one(db_session)
    html = b"<html><body>same</body></html>"
    client = _mock_client(html=html)

    first = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=client,
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    second = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=client,
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()

    assert first.status == STATUS_FETCHED_NEW_VERSION
    assert second.status == STATUS_UNCHANGED
    count = db_session.scalar(select(func.count()).select_from(DocumentVersion))
    assert count == 1


def test_changed_content_creates_new_version_preserves_old(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_one(db_session)

    client1 = _mock_client(html=b"<html>one</html>")
    r1 = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=client1,
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()

    client2 = _mock_client(html=b"<html>two</html>")
    r2 = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=client2,
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()

    assert r1.status == STATUS_FETCHED_NEW_VERSION
    assert r2.status == STATUS_FETCHED_NEW_VERSION
    assert r2.version_number == 2
    versions = list(db_session.scalars(select(DocumentVersion)).all())
    assert len(versions) == 2
    current = [v for v in versions if v.is_current]
    assert len(current) == 1
    assert current[0].version_number == 2
    assert (tmp_path / r1.storage_path).read_bytes() == b"<html>one</html>"
    assert (tmp_path / r2.storage_path).read_bytes() == b"<html>two</html>"


def test_failed_http_does_not_create_version(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_one(db_session)
    client = _mock_client()
    client.stream.return_value = _stream_response(status_code=503, body=b"down")

    result = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=client,
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()

    assert result.status == STATUS_FAILED
    assert db_session.scalar(select(func.count()).select_from(DocumentVersion)) == 0


def test_non_html_rejected(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_one(db_session)
    client = _mock_client(
        html=b"%PDF-1.4",
        content_type="application/pdf",
    )
    result = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=client,
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    assert result.status == STATUS_FAILED
    assert "non-HTML" in result.message


def test_redirect_outside_scope_blocked():
    policy = fetcher_policy()
    patterns = ["https://www.gov.uk/skilled-worker-visa*"]

    redirect_cm = _stream_response(
        status_code=302,
        headers={"location": "https://evil.example/phishing"},
        body=b"",
    )
    client = MagicMock()
    client.get.return_value = MagicMock(
        status_code=200, text="User-agent: *\nAllow: /\n"
    )
    client.stream.return_value = redirect_cm

    result = fetch_html_seed(
        "https://www.gov.uk/skilled-worker-visa",
        allowed_patterns=patterns,
        policy=policy,
        client=client,
    )
    assert result.ok is False
    assert "allowlist" in (result.error or "")


def test_candidate_source_skipped(db_session, tmp_path: Path):
    _clear_registry(db_session)
    import_seed_sources(db_session, verify=False)
    db_session.flush()
    source = db_session.scalar(select(Source).limit(1))
    assert source is not None
    assert source.review_status == "candidate"

    result = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=_mock_client(),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    assert result.status == STATUS_SKIPPED


def test_fetch_approved_seeds_single_source(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_one(db_session)
    # Approve a second source too.
    other = db_session.scalar(
        select(Source).where(Source.id != source.id).limit(1)
    )
    assert other is not None
    set_source_review_status(db_session, source_id=other.id, status="approved")
    db_session.flush()

    report = fetch_approved_seeds(
        db_session,
        persist=True,
        source_id=source.id,
        http_client=_mock_client(html=b"<html>only-one</html>"),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    assert len(report.results) == 1
    assert report.results[0].source_id == source.id
    assert report.fetched_new == 1
