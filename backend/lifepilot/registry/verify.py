"""Lightweight reachability checks for seed registry URLs.

HTTP verification reports connectivity and final-host information only.
It must never be treated as content authenticity or as source approval.
robots.txt, terms, and rate limits remain separate crawler concerns.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import certifi
import httpx

from lifepilot.registry.urls import host_of, validate_http_url

DEFAULT_USER_AGENT = (
    "LifePilotSeedVerifier/0.1 "
    "(+https://github.com/AshirRaza/LifePilot; "
    "official-seed reachability check only; not a crawler)"
)
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}


@dataclass(frozen=True)
class VerificationResult:
    seed_url: str
    connectivity_ok: bool
    landed_on_expected_host: bool
    status_code: int | None
    final_url: str | None
    final_host: str | None
    redirected: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Reachability to an expected host succeeded.

        This is NOT an approval signal and must not change review_status.
        """
        return self.connectivity_ok and self.landed_on_expected_host

    @property
    def summary(self) -> str:
        return (
            f"connectivity_ok={self.connectivity_ok} "
            f"expected_host={self.landed_on_expected_host} "
            f"status={self.status_code} "
            f"final_host={self.final_host} "
            f"redirected={self.redirected}"
            + (f" error={self.error}" if self.error else "")
        )


@dataclass(frozen=True)
class _ProbeSnapshot:
    status_code: int
    url: str


def _host_allowed(host: str | None, expected_hosts: tuple[str, ...]) -> bool:
    if not host:
        return False
    host = host.lower()
    allowed = {h.lower() for h in expected_hosts}
    if host in allowed:
        return True
    # Accept apex/www variants when either form is listed.
    if host.startswith("www.") and host[4:] in allowed:
        return True
    if f"www.{host}" in allowed:
        return True
    return False


def _snapshot_from_response(response: httpx.Response) -> _ProbeSnapshot:
    return _ProbeSnapshot(status_code=response.status_code, url=str(response.url))


def _probe(client: httpx.Client, seed_url: str) -> _ProbeSnapshot:
    """Probe URL without downloading the full body."""
    try:
        head_response = client.head(seed_url)
        if (
            head_response.status_code not in {403, 405, 501}
            and head_response.status_code < 400
        ):
            return _snapshot_from_response(head_response)
    except httpx.HTTPError:
        pass

    # Streamed GET: read status/headers only, never the body.
    with client.stream("GET", seed_url) as response:
        snapshot = _snapshot_from_response(response)
        response.close()
        return snapshot


def verify_seed_url(
    seed_url: str,
    expected_hosts: tuple[str, ...],
    *,
    timeout: float = 30.0,
    client: httpx.Client | None = None,
) -> VerificationResult:
    """Check that a seed URL is reachable and stays on an expected host.

    Distinguishes connectivity success from content authenticity. A successful
    result must not be used to approve a source.
    """
    try:
        # Allow http during reachability probes; approval still requires https.
        validate_http_url(seed_url, require_https=False, allow_http=True)
    except ValueError as exc:
        return VerificationResult(
            seed_url=seed_url,
            connectivity_ok=False,
            landed_on_expected_host=False,
            status_code=None,
            final_url=None,
            final_host=host_of(seed_url),
            error=str(exc),
        )

    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            verify=certifi.where(),
            headers=DEFAULT_HEADERS,
        )

    declared_host = host_of(seed_url)
    last_error: str | None = None
    try:
        for attempt in range(3):
            try:
                probe = _probe(client, seed_url)
                final_url = probe.url
                final_host = urlsplit(final_url).hostname
                host_ok = _host_allowed(final_host, expected_hosts)
                redirected = final_url.rstrip("/") != seed_url.rstrip("/")
                connectivity_ok = 200 <= probe.status_code < 400
                return VerificationResult(
                    seed_url=seed_url,
                    connectivity_ok=connectivity_ok,
                    landed_on_expected_host=host_ok,
                    status_code=probe.status_code,
                    final_url=final_url,
                    final_host=final_host.lower() if final_host else None,
                    redirected=redirected,
                    error=(
                        None
                        if connectivity_ok and host_ok
                        else "non-success status or left expected host"
                    ),
                )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                break

        return VerificationResult(
            seed_url=seed_url,
            connectivity_ok=False,
            landed_on_expected_host=_host_allowed(declared_host, expected_hosts),
            status_code=None,
            final_url=None,
            final_host=declared_host,
            error=last_error or "verification failed",
        )
    finally:
        if owns_client:
            client.close()
