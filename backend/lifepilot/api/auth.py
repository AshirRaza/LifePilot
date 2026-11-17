"""Service authentication for the private/internal API."""

from __future__ import annotations

import hashlib
import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from lifepilot.api.settings import get_api_settings


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def verify_api_key(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> str | None:
    """Validate Bearer or X-API-Key credentials when auth is required."""
    cfg = get_api_settings()
    presented = (x_api_key or "").strip() or _extract_bearer(authorization)
    if not cfg.require_auth:
        return presented
    if not cfg.api_keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is required but no keys are configured",
        )
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    for idx, key in enumerate(cfg.api_keys):
        if hmac.compare_digest(presented, key):
            # Stable non-secret principal for rate limiting / logs.
            digest = hmac.new(
                b"lifepilot-api-principal",
                key.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()[:12]
            return f"key_{idx}_{digest}"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


AuthPrincipal = Annotated[str | None, Depends(verify_api_key)]
