"""Apply approved Phase 6H decisions via the explicit registry review helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.coverage.source_expansion.candidates import (
    PLAN_EXACT_ALLOWLIST,
    PLAN_EXISTING_SCOPE,
    PLAN_NEW_SOURCE,
    Candidate,
)
from lifepilot.coverage.source_expansion.validate import Decision
from lifepilot.db.models import Authority, Jurisdiction, Source
from lifepilot.registry.review import (
    set_source_allowed_patterns,
    set_source_review_status,
)
from lifepilot.registry.urls import (
    InvalidRegistryUrlError,
    normalize_url,
    parse_allowed_pattern,
    path_prefix_pattern,
)


class UnsafeScopeError(ValueError):
    """Raised when a pattern would broaden scope beyond narrow path/exact rules."""


def is_narrow_pattern(pattern: str) -> bool:
    """True for exact URLs or ``path*`` with at least one path segment.

    Rejects whole-host wildcards such as ``https://host/*`` or ``https://host*``.
    """
    try:
        parsed = parse_allowed_pattern(pattern)
    except InvalidRegistryUrlError:
        return False  # mid-string or host wildcards are never narrow
    if parsed.recursive:
        segments = [s for s in parsed.path.split("/") if s]
        return len(segments) >= 1
    return parsed.path not in {"", "/"}


def assert_narrow_patterns(patterns: list[str]) -> None:
    for p in patterns:
        if not is_narrow_pattern(p):
            raise UnsafeScopeError(f"Pattern is not narrow enough: {p!r}")


@dataclass
class ApprovalAction:
    candidate_id: str
    source_id: int | None
    action: str
    patterns_before: list[str] = field(default_factory=list)
    patterns_after: list[str] = field(default_factory=list)
    review_status_before: str | None = None
    review_status_after: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_id": self.source_id,
            "action": self.action,
            "patterns_before": self.patterns_before,
            "patterns_after": self.patterns_after,
            "review_status_before": self.review_status_before,
            "review_status_after": self.review_status_after,
            "note": self.note,
        }


def _exact_pattern(url: str) -> str:
    """Exact allowlist entry (non-recursive, no trailing '*')."""
    return normalize_url(url)


def _ensure_exact_patterns(
    session: Session,
    *,
    source_id: int,
    urls: list[str],
    candidate_ids: list[str],
) -> ApprovalAction:
    source = session.get(Source, source_id)
    if source is None:
        raise ValueError(f"source {source_id} not found")
    before = list(source.allowed_url_patterns or [])
    wanted = [_exact_pattern(u) for u in urls]
    assert_narrow_patterns(wanted)
    after = list(before)
    for pat in wanted:
        if pat not in after:
            after.append(pat)
    if after == before:
        return ApprovalAction(
            candidate_id=",".join(candidate_ids),
            source_id=source_id,
            action="already_registered",
            patterns_before=before,
            patterns_after=after,
            review_status_before=source.review_status,
            review_status_after=source.review_status,
        )
    set_source_allowed_patterns(
        session,
        source_id=source_id,
        patterns=after,
        confirm_scope_expansion=True,
    )
    session.refresh(source)
    return ApprovalAction(
        candidate_id=",".join(candidate_ids),
        source_id=source_id,
        action="exact_patterns_added",
        patterns_before=before,
        patterns_after=list(source.allowed_url_patterns or []),
        review_status_before=source.review_status,
        review_status_after=source.review_status,
        note="existing wildcard pattern retained; exact URLs appended with confirm_scope_expansion",
    )


def _validate_authority(session: Session, spec_authority_id: int, spec_jurisdiction_id: int) -> None:
    authority = session.get(Authority, spec_authority_id)
    juris = session.get(Jurisdiction, spec_jurisdiction_id)
    if authority is None or juris is None:
        raise ValueError(
            f"authority {spec_authority_id} or jurisdiction {spec_jurisdiction_id} missing"
        )
    if authority.jurisdiction_id != juris.id:
        raise ValueError(
            f"authority {authority.id} belongs to jurisdiction {authority.jurisdiction_id}, "
            f"not {juris.id}"
        )


def _create_or_get_source(
    session: Session, candidate: Candidate, decision: Decision
) -> ApprovalAction:
    spec = candidate.new_source
    assert spec is not None
    seed_norm = normalize_url(spec.seed_url)
    existing = session.scalar(select(Source).where(Source.seed_url_normalized == seed_norm))
    patterns = [path_prefix_pattern(spec.seed_url)]
    assert_narrow_patterns(patterns)
    if existing is not None:
        before_status = existing.review_status
        if existing.review_status != "approved":
            set_source_review_status(session, status="approved", source_id=existing.id)
        return ApprovalAction(
            candidate_id=candidate.candidate_id,
            source_id=existing.id,
            action="already_registered" if before_status == "approved" else "approved_existing_source",
            patterns_before=list(existing.allowed_url_patterns or []),
            patterns_after=list(existing.allowed_url_patterns or []),
            review_status_before=before_status,
            review_status_after=existing.review_status,
        )
    _validate_authority(session, spec.authority_id, spec.jurisdiction_id)
    source = Source(
        authority_id=spec.authority_id,
        jurisdiction_id=spec.jurisdiction_id,
        name=spec.name,
        seed_url=spec.seed_url,
        seed_url_normalized=seed_norm,
        allowed_url_patterns=patterns,
        review_status="candidate",
        default_language=spec.default_language,
        notes=(spec.notes or "Phase 6H owner-authorized source")
        + f" | decision={decision.decision_code} at {decision.decided_at}",
    )
    session.add(source)
    session.flush()
    set_source_review_status(session, status="approved", source_id=source.id)
    return ApprovalAction(
        candidate_id=candidate.candidate_id,
        source_id=source.id,
        action="source_created_and_approved",
        patterns_before=[],
        patterns_after=list(source.allowed_url_patterns or []),
        review_status_before="candidate",
        review_status_after=source.review_status,
    )


def apply_decisions(
    session: Session,
    candidates: list[Candidate],
    decisions: list[Decision],
) -> dict[str, ApprovalAction]:
    """Apply approved decisions; returns actions keyed by candidate_id.

    Rejected candidates never touch the DB. Caller commits.
    """
    by_id = {c.candidate_id: c for c in candidates}
    actions: dict[str, ApprovalAction] = {}

    exact_by_source: dict[int, list[Decision]] = {}
    for d in decisions:
        if d.decision != "approved":
            continue
        cand = by_id[d.candidate_id]
        if cand.plan == PLAN_EXACT_ALLOWLIST and cand.source_id is not None:
            exact_by_source.setdefault(cand.source_id, []).append(d)
        elif cand.plan == PLAN_NEW_SOURCE:
            actions[d.candidate_id] = _create_or_get_source(session, cand, d)
            d.source_id = actions[d.candidate_id].source_id
        elif cand.plan == PLAN_EXISTING_SCOPE:
            src = session.get(Source, cand.source_id) if cand.source_id else None
            actions[d.candidate_id] = ApprovalAction(
                candidate_id=d.candidate_id,
                source_id=cand.source_id,
                action="no_change_required",
                patterns_before=list(src.allowed_url_patterns or []) if src else [],
                patterns_after=list(src.allowed_url_patterns or []) if src else [],
                review_status_before=src.review_status if src else None,
                review_status_after=src.review_status if src else None,
                note="URL already within existing allowlist",
            )

    for source_id, group in exact_by_source.items():
        urls = [d.registration_url or d.authorized_url for d in group]
        action = _ensure_exact_patterns(
            session,
            source_id=source_id,
            urls=urls,
            candidate_ids=[d.candidate_id for d in group],
        )
        for d in group:
            actions[d.candidate_id] = ApprovalAction(
                candidate_id=d.candidate_id,
                source_id=source_id,
                action=action.action,
                patterns_before=action.patterns_before,
                patterns_after=action.patterns_after,
                review_status_before=action.review_status_before,
                review_status_after=action.review_status_after,
                note=action.note,
            )
    return actions
