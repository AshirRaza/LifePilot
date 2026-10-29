"""Tests for HTML content-quality validation and blocked persist behavior."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy import delete, func, select

from lifepilot.crawl.fetcher import (
    STATUS_BLOCKED_BY_BOT_PROTECTION,
    STATUS_FETCHED_NEW_VERSION,
    STATUS_INVALID_CONTENT,
    fetch_approved_seed,
)
from lifepilot.crawl.html_quality import (
    OUTCOME_BLOCKED_BY_BOT_PROTECTION,
    OUTCOME_INVALID_CONTENT,
    OUTCOME_OK,
    assess_html_content,
)
from lifepilot.crawl.policy import fetcher_policy
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


RADWARE_HTML = b"""<!DOCTYPE html>
<html lang="en"><head>
<title>Radware Page</title>
<meta name="description" content="Loader page." />
<meta property="og:title" content="Radware Page" />
<meta property="og:url" content="https://radware.com/" />
</head><body><div class="container">loading</div></body></html>
"""

GOV_HTML = b"""<!DOCTYPE html>
<html lang="en"><head>
<title>Skilled Worker visa - GOV.UK</title>
<meta name="description" content="Apply for a Skilled Worker visa" />
</head><body>
<main><h1>Skilled Worker visa</h1>
<a href="/skilled-worker-visa/eligibility">Eligibility</a>
<p>Official guidance about visas and security of employment rights.</p>
</main></body></html>
"""

ACCESS_DENIED_HTML = b"""<!DOCTYPE html>
<html><head><title>Access Denied</title></head>
<body><h1>Access Denied</h1>
<p>You don't have permission to access this resource on this server.</p>
</body></html>
"""

AMBIGUOUS_HTML = b"""<!DOCTYPE html>
<html><head><title>Immigration security checks guidance</title></head>
<body><main>
<h1>Security checks for applicants</h1>
<p>This page explains access to official guidance about security vetting.</p>
<a href="/guidance/security-checks">More</a>
</main></body></html>
"""


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
    body: bytes = GOV_HTML,
):
    stream = MagicMock()
    stream.status_code = status_code
    stream.headers = headers or {"content-type": "text/html; charset=utf-8"}
    stream.iter_bytes.side_effect = lambda: iter([body])
    stream.close = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = stream
    cm.__exit__.return_value = False
    return cm


def _mock_client(*, html: bytes) -> MagicMock:
    client = MagicMock()
    client.get.return_value = MagicMock(
        status_code=200, text="User-agent: *\nAllow: /\n"
    )
    client.stream.return_value = _stream_response(body=html)
    return client


def test_assess_accepts_normal_government_html():
    result = assess_html_content(GOV_HTML)
    assert result.ok is True
    assert result.outcome == OUTCOME_OK


def test_assess_detects_radware_challenge():
    result = assess_html_content(RADWARE_HTML)
    assert result.ok is False
    assert result.outcome == OUTCOME_BLOCKED_BY_BOT_PROTECTION
    assert "title_radware_page" in result.reasons
    assert "og_url_radware" in result.reasons


def test_assess_detects_access_denied_cluster():
    result = assess_html_content(ACCESS_DENIED_HTML)
    assert result.ok is False
    assert result.outcome == OUTCOME_BLOCKED_BY_BOT_PROTECTION


def test_assess_does_not_flag_ambiguous_security_wording():
    result = assess_html_content(AMBIGUOUS_HTML)
    assert result.ok is True
    assert result.outcome == OUTCOME_OK


def test_assess_empty_body_invalid():
    result = assess_html_content(b"   ")
    assert result.ok is False
    assert result.outcome == OUTCOME_INVALID_CONTENT


def test_persist_rejects_radware_without_creating_version(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_one(db_session)
    result = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=_mock_client(html=RADWARE_HTML),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    assert result.status == STATUS_BLOCKED_BY_BOT_PROTECTION
    assert db_session.scalar(select(func.count()).select_from(Document)) == 0
    assert db_session.scalar(select(func.count()).select_from(DocumentVersion)) == 0
    assert list(tmp_path.rglob("*.html")) == []


def test_blocked_fetch_preserves_existing_valid_version(db_session, tmp_path: Path):
    _clear_registry(db_session)
    source = _approve_one(db_session)
    first = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=_mock_client(html=GOV_HTML),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    assert first.status == STATUS_FETCHED_NEW_VERSION
    doc_id = first.document_id
    assert doc_id is not None
    version_count = db_session.scalar(select(func.count()).select_from(DocumentVersion))
    assert version_count == 1
    stored = tmp_path / first.storage_path
    assert stored.read_bytes() == GOV_HTML

    blocked = fetch_approved_seed(
        db_session,
        source.id,
        persist=True,
        http_client=_mock_client(html=RADWARE_HTML),
        storage_root=tmp_path,
        policy=fetcher_policy(),
    )
    db_session.flush()
    assert blocked.status == STATUS_BLOCKED_BY_BOT_PROTECTION
    assert blocked.document_id == doc_id
    assert db_session.scalar(select(func.count()).select_from(DocumentVersion)) == 1
    current = db_session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == doc_id,
            DocumentVersion.is_current.is_(True),
        )
    )
    assert current is not None
    assert current.version_number == 1
    assert stored.read_bytes() == GOV_HTML
