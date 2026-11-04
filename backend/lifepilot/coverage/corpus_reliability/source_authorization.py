"""Exact-URL source authorization helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.coverage.source_expansion.approve import assert_narrow_patterns
from lifepilot.db.models import Source
from lifepilot.registry.review import set_source_review_status
from lifepilot.registry.urls import normalize_url, url_in_any_allowed_pattern

EXPLICITLY_NOT_APPROVED = (
    "https://icp.gov.ae/en/services",
    "https://icp.gov.ae/en/services/",
)

DEFAULT_APPROVAL_NOTE = (
    "Owner-approved exact URL only. "
    "No domain/wildcard expansion; no recursive crawl; redirects not auto-approved."
)


def exact_pattern(url: str) -> str:
    return normalize_url(url)


_exact_pattern = exact_pattern


def classify_url_authorization(session: Session, url: str) -> dict[str, Any]:
    nu = normalize_url(url)
    sources = session.scalars(select(Source).where(Source.review_status == "approved")).all()
    for src in sources:
        pats = list(src.allowed_url_patterns or [])
        if url_in_any_allowed_pattern(nu, pats):
            return {
                "authorization_status": "already_authorized",
                "source_id": src.id,
                "source_name": src.name,
                "authorized_source_id": src.id,
                "authorized_source_name": src.name,
            }
    return {
        "authorization_status": "requires_owner_approval",
        "source_id": "",
        "source_name": "",
        "authorized_source_id": "",
        "authorized_source_name": "",
    }


def register_exact_approved_source(
    session: Session,
    target: dict[str, Any],
    *,
    approval_note: str = DEFAULT_APPROVAL_NOTE,
    phase_tag: str = "exact_approval",
) -> dict[str, Any]:
    """Create or reuse an approved source whose allowlist is ONLY the exact URL."""
    url = target["url"]
    exact = exact_pattern(url)
    assert_narrow_patterns([exact])
    seed_norm = exact

    existing = session.scalar(
        select(Source).where(Source.seed_url_normalized == seed_norm)
    )
    if existing is not None:
        before_patterns = list(existing.allowed_url_patterns or [])
        before_status = existing.review_status
        if before_patterns != [exact]:
            existing.allowed_url_patterns = [exact]
            session.add(existing)
            session.flush()
        if existing.review_status != "approved":
            set_source_review_status(session, status="approved", source_id=existing.id)
        note = (existing.notes or "").strip()
        if phase_tag not in note:
            existing.notes = (note + " | " if note else "") + approval_note
            session.add(existing)
            session.flush()
        session.refresh(existing)
        return {
            "key": target.get("key") or exact,
            "url": exact,
            "source_id": existing.id,
            "action": "reused_exact_source",
            "review_status_before": before_status,
            "review_status_after": existing.review_status,
            "patterns_before": before_patterns,
            "patterns_after": list(existing.allowed_url_patterns or []),
            "note": approval_note,
        }

    source = Source(
        authority_id=int(target["authority_id"]),
        jurisdiction_id=int(target["jurisdiction_id"]),
        name=target["source_name"],
        seed_url=url,
        seed_url_normalized=seed_norm,
        allowed_url_patterns=[exact],
        review_status="candidate",
        default_language=target.get("default_language") or "en",
        notes=approval_note,
    )
    session.add(source)
    session.flush()
    set_source_review_status(session, status="approved", source_id=source.id)
    session.refresh(source)
    return {
        "key": target.get("key") or exact,
        "url": exact,
        "source_id": source.id,
        "action": "created_exact_source_and_approved",
        "review_status_before": "candidate",
        "review_status_after": source.review_status,
        "patterns_before": [],
        "patterns_after": list(source.allowed_url_patterns or []),
        "note": approval_note,
    }


def is_explicitly_not_approved(url: str) -> bool:
    try:
        nu = normalize_url(url).rstrip("/")
    except Exception:
        nu = (url or "").rstrip("/")
    return any(nu == normalize_url(x).rstrip("/") for x in EXPLICITLY_NOT_APPROVED)
