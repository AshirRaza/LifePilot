"""Server-side reviewer identity and authorization (Phase 10D.2).

Fail-closed by default. Client-supplied actor IDs are never trusted.
Service API key principals are never treated as human reviewers.

Production human authentication is intentionally not implemented here
(Phase 14 / future IdP). Until a trusted provider is configured:

- ``auth_mode=disabled`` → no human identities; decision ops unavailable
- ``auth_mode=test_fixture`` → explicit test-only reviewer table from env
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from lifepilot.db.legal_review import ActorRef

PERM_LEGAL_REVIEW_READ = "legal_review.read"
PERM_LEGAL_REVIEW_ASSIGN = "legal_review.assign"
PERM_LEGAL_REVIEW_DECIDE = "legal_review.decide"
PERM_LEGAL_REVIEW_ADMIN = "legal_review.admin"

ALL_LEGAL_REVIEW_PERMS = frozenset(
    {
        PERM_LEGAL_REVIEW_READ,
        PERM_LEGAL_REVIEW_ASSIGN,
        PERM_LEGAL_REVIEW_DECIDE,
        PERM_LEGAL_REVIEW_ADMIN,
    }
)

AUTH_MODE_DISABLED = "disabled"
AUTH_MODE_TEST_FIXTURE = "test_fixture"
VALID_AUTH_MODES = frozenset({AUTH_MODE_DISABLED, AUTH_MODE_TEST_FIXTURE})


class ReviewerAuthError(PermissionError):
    """Authorization failure for legal review operations."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReviewerIdentity:
    """Authenticated reviewer (or authenticated non-human principal)."""

    actor_id: str
    actor_kind: str
    permissions: frozenset[str]
    display_name: str | None = None

    def has_permission(self, permission: str) -> bool:
        if permission in self.permissions:
            return True
        # Admin implies all legal-review permissions.
        if PERM_LEGAL_REVIEW_ADMIN in self.permissions and permission in ALL_LEGAL_REVIEW_PERMS:
            return True
        return False

    def require_permission(self, permission: str) -> None:
        if not self.has_permission(permission):
            raise ReviewerAuthError(
                f"missing permission {permission}",
                code="permission_denied",
            )

    def as_actor(self) -> ActorRef:
        return ActorRef(actor_id=self.actor_id, actor_kind=self.actor_kind)


class ReviewerAuthProvider(Protocol):
    """Trusted server-side identity provider for legal review."""

    @property
    def mode(self) -> str: ...

    @property
    def human_auth_available(self) -> bool: ...

    def resolve_human_reviewer(
        self,
        *,
        reviewer_id: str | None,
        reviewer_secret: str | None,
    ) -> ReviewerIdentity: ...


@dataclass(frozen=True)
class _FixtureReviewer:
    actor_id: str
    secret: str
    permissions: frozenset[str]
    display_name: str | None = None


class FailClosedReviewerAuthProvider:
    """Production-safe default: no human reviewers can be resolved."""

    @property
    def mode(self) -> str:
        return AUTH_MODE_DISABLED

    @property
    def human_auth_available(self) -> bool:
        return False

    def resolve_human_reviewer(
        self,
        *,
        reviewer_id: str | None,
        reviewer_secret: str | None,
    ) -> ReviewerIdentity:
        del reviewer_id, reviewer_secret
        raise ReviewerAuthError(
            "Human legal-review authentication is not configured",
            code="human_auth_unavailable",
        )


class TestFixtureReviewerAuthProvider:
    """Explicitly test-only reviewer table loaded from environment.

    Format (comma-separated entries)::

        LIFEPILOT_LEGAL_REVIEW_TEST_REVIEWERS=
          reviewer:alice:secretA:legal_review.read|legal_review.assign|legal_review.decide,
          reviewer:admin:secretB:legal_review.admin

    Never enable ``auth_mode=test_fixture`` in production.
    """

    def __init__(self, reviewers: tuple[_FixtureReviewer, ...]) -> None:
        if not reviewers:
            raise ReviewerAuthError(
                "test_fixture mode requires LIFEPILOT_LEGAL_REVIEW_TEST_REVIEWERS",
                code="test_fixture_misconfigured",
            )
        self._by_id = {r.actor_id: r for r in reviewers}

    @property
    def mode(self) -> str:
        return AUTH_MODE_TEST_FIXTURE

    @property
    def human_auth_available(self) -> bool:
        return True

    def resolve_human_reviewer(
        self,
        *,
        reviewer_id: str | None,
        reviewer_secret: str | None,
    ) -> ReviewerIdentity:
        rid = (reviewer_id or "").strip()
        secret = (reviewer_secret or "").strip()
        if not rid or not secret:
            raise ReviewerAuthError(
                "reviewer identity credentials required",
                code="missing_reviewer_credentials",
            )
        entry = self._by_id.get(rid)
        if entry is None or not hmac.compare_digest(entry.secret, secret):
            raise ReviewerAuthError(
                "invalid reviewer credentials",
                code="invalid_reviewer_credentials",
            )
        return ReviewerIdentity(
            actor_id=entry.actor_id,
            actor_kind="human_reviewer",
            permissions=entry.permissions,
            display_name=entry.display_name,
        )


def _parse_test_reviewers(raw: str) -> tuple[_FixtureReviewer, ...]:
    """Parse ``id:secret:perm|perm`` entries (id may contain colons like reviewer:alice).

    Split from the right: permissions, secret, then remainder as actor_id.
    """
    out: list[_FixtureReviewer] = []
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        # id:secret:perms — allow colons in id by rsplit
        try:
            id_and_secret, perms_raw = entry.rsplit(":", 1)
            actor_id, secret = id_and_secret.rsplit(":", 1)
        except ValueError as exc:
            raise ReviewerAuthError(
                f"invalid test reviewer entry: {entry!r}",
                code="test_fixture_misconfigured",
            ) from exc
        actor_id = actor_id.strip()
        secret = secret.strip()
        perms = frozenset(p.strip() for p in perms_raw.split("|") if p.strip())
        if not actor_id or not secret:
            raise ReviewerAuthError(
                "test reviewer id and secret required",
                code="test_fixture_misconfigured",
            )
        unknown = perms - ALL_LEGAL_REVIEW_PERMS
        if unknown:
            raise ReviewerAuthError(
                f"unknown permissions: {sorted(unknown)}",
                code="test_fixture_misconfigured",
            )
        if not perms:
            raise ReviewerAuthError(
                "test reviewer requires at least one permission",
                code="test_fixture_misconfigured",
            )
        out.append(
            _FixtureReviewer(
                actor_id=actor_id,
                secret=secret,
                permissions=perms,
            )
        )
    return tuple(out)


def resolve_auth_mode(environ: dict[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    raw = (env.get("LIFEPILOT_LEGAL_REVIEW_AUTH_MODE") or AUTH_MODE_DISABLED).strip().lower()
    if raw not in VALID_AUTH_MODES:
        return AUTH_MODE_DISABLED
    # Hard fail-closed: never allow test_fixture in production.
    life_env = (env.get("LIFEPILOT_ENV") or "development").strip().lower()
    if raw == AUTH_MODE_TEST_FIXTURE and life_env in {"production", "prod"}:
        return AUTH_MODE_DISABLED
    return raw


@lru_cache(maxsize=1)
def get_reviewer_auth_provider() -> ReviewerAuthProvider:
    mode = resolve_auth_mode()
    if mode == AUTH_MODE_TEST_FIXTURE:
        raw = (os.environ.get("LIFEPILOT_LEGAL_REVIEW_TEST_REVIEWERS") or "").strip()
        reviewers = _parse_test_reviewers(raw)
        return TestFixtureReviewerAuthProvider(reviewers)
    return FailClosedReviewerAuthProvider()


def reset_reviewer_auth_provider_cache() -> None:
    get_reviewer_auth_provider.cache_clear()


def reject_client_supplied_actor(
    *,
    client_actor_id: str | None,
    client_actor_kind: str | None,
) -> None:
    """Defense-in-depth: request bodies must not carry impersonation fields."""
    if client_actor_id is not None or client_actor_kind is not None:
        raise ReviewerAuthError(
            "client-supplied actor identity is forbidden",
            code="forged_actor_identity",
        )
