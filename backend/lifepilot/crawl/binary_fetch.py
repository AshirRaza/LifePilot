"""Redirect-safe streaming fetch for in-scope PDF/DOCX URLs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin

import certifi
import httpx

from lifepilot.crawl.binary_validate import validate_binary_bytes
from lifepilot.crawl.content_type import content_type_base
from lifepilot.crawl.net_safety import url_has_blocked_fetch_host
from lifepilot.crawl.policy import CrawlPolicy
from lifepilot.crawl.robots import check_robots
from lifepilot.db.temporal import extract_http_header_temporal
from lifepilot.registry.urls import url_in_any_allowed_pattern

_PDF_TYPES = {"application/pdf", "application/x-pdf"}
_DOCX_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/octet-stream",  # common for gov CDNs; magic still required
}


@dataclass
class BinaryFetchResult:
    ok: bool
    requested_url: str
    expected_kind: str
    final_url: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    body: bytes | None = None
    content_kind: str | None = None
    redirect_chain: list[str] = field(default_factory=list)
    error: str | None = None
    http_last_modified: datetime | None = None
    http_etag: str | None = None
    retrieved_at: datetime | None = None
    temporal_warnings: list[str] = field(default_factory=list)
    temporal_observations: list[dict] = field(default_factory=list)


def _content_type_plausible(kind: str, content_type: str | None) -> bool:
    base = content_type_base(content_type)
    if base is None:
        # Some servers omit Content-Type; rely on magic later.
        return True
    if kind == "pdf":
        return base in _PDF_TYPES or base == "application/octet-stream"
    if kind == "docx":
        return base in _DOCX_TYPES
    return False


def fetch_binary_url(
    url: str,
    *,
    expected_kind: str,
    allowed_patterns: list[str],
    policy: CrawlPolicy,
    client: httpx.Client | None = None,
) -> BinaryFetchResult:
    """GET ``url`` as PDF/DOCX with allowlist, robots, redirect, and magic checks."""
    if expected_kind not in {"pdf", "docx"}:
        return BinaryFetchResult(
            ok=False,
            requested_url=url,
            expected_kind=expected_kind,
            error=f"unsupported expected_kind={expected_kind!r}",
        )

    owns_client = client is None
    timeout = policy.binary_request_timeout_seconds
    if client is None:
        accept = (
            "application/pdf,application/octet-stream;q=0.8,*/*;q=0.1"
            if expected_kind == "pdf"
            else (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document,application/octet-stream;q=0.8,*/*;q=0.1"
            )
        )
        client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            verify=certifi.where(),
            headers={
                "User-Agent": policy.user_agent,
                "Accept": accept,
            },
        )

    current = url
    chain: list[str] = []
    try:
        for _ in range(policy.max_redirects + 1):
            if url_has_blocked_fetch_host(current):
                return BinaryFetchResult(
                    ok=False,
                    requested_url=url,
                    expected_kind=expected_kind,
                    final_url=current,
                    redirect_chain=chain,
                    error="blocked host (private/loopback/metadata)",
                )
            if not url_in_any_allowed_pattern(current, allowed_patterns):
                return BinaryFetchResult(
                    ok=False,
                    requested_url=url,
                    expected_kind=expected_kind,
                    final_url=current,
                    redirect_chain=chain,
                    error="URL outside approved allowlist scope",
                )

            robots = check_robots(current, policy, client=client)
            if policy.require_robots_ok and not robots.fetched:
                return BinaryFetchResult(
                    ok=False,
                    requested_url=url,
                    expected_kind=expected_kind,
                    final_url=current,
                    redirect_chain=chain,
                    error=f"robots.txt unavailable: {robots.error}",
                )
            if robots.can_fetch is False:
                return BinaryFetchResult(
                    ok=False,
                    requested_url=url,
                    expected_kind=expected_kind,
                    final_url=current,
                    redirect_chain=chain,
                    error="robots.txt disallows fetch for this URL",
                )

            with client.stream("GET", current) as response:
                status = response.status_code
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    response.close()
                    if not location:
                        return BinaryFetchResult(
                            ok=False,
                            requested_url=url,
                            expected_kind=expected_kind,
                            final_url=current,
                            status_code=status,
                            redirect_chain=chain,
                            error="redirect without Location header",
                        )
                    next_url = urljoin(current, location)
                    chain.append(next_url)
                    current = next_url
                    continue

                if status >= 400:
                    response.close()
                    return BinaryFetchResult(
                        ok=False,
                        requested_url=url,
                        expected_kind=expected_kind,
                        final_url=current,
                        status_code=status,
                        redirect_chain=chain,
                        error=f"HTTP {status}",
                    )

                content_type = response.headers.get("content-type")
                if not _content_type_plausible(expected_kind, content_type):
                    response.close()
                    return BinaryFetchResult(
                        ok=False,
                        requested_url=url,
                        expected_kind=expected_kind,
                        final_url=current,
                        status_code=status,
                        content_type=content_type,
                        redirect_chain=chain,
                        error=f"implausible content-type for {expected_kind}: {content_type!r}",
                    )

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > policy.max_binary_response_bytes:
                        response.close()
                        return BinaryFetchResult(
                            ok=False,
                            requested_url=url,
                            expected_kind=expected_kind,
                            final_url=current,
                            status_code=status,
                            content_type=content_type,
                            redirect_chain=chain,
                            error=(
                                "response exceeds max_binary_response_bytes="
                                f"{policy.max_binary_response_bytes}"
                            ),
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
                retrieved_at = datetime.now(timezone.utc)
                http_temporal = extract_http_header_temporal(
                    response.headers, retrieved_at=retrieved_at
                )

            validation = validate_binary_bytes(
                body,
                expected_kind=expected_kind,
                max_uncompressed_bytes=policy.max_docx_uncompressed_bytes,
            )
            if not validation.ok:
                return BinaryFetchResult(
                    ok=False,
                    requested_url=url,
                    expected_kind=expected_kind,
                    final_url=current,
                    status_code=status,
                    content_type=content_type,
                    redirect_chain=chain,
                    error=f"binary validation failed: {validation.reason}",
                )

            return BinaryFetchResult(
                ok=True,
                requested_url=url,
                expected_kind=expected_kind,
                final_url=current,
                status_code=status,
                content_type=content_type,
                body=body,
                content_kind=validation.content_kind,
                redirect_chain=chain,
                http_last_modified=http_temporal.http_last_modified,
                http_etag=http_temporal.http_etag,
                retrieved_at=http_temporal.retrieved_at,
                temporal_warnings=http_temporal.warnings,
                temporal_observations=http_temporal.observations,
            )

        return BinaryFetchResult(
            ok=False,
            requested_url=url,
            expected_kind=expected_kind,
            final_url=current,
            redirect_chain=chain,
            error=f"too many redirects (max={policy.max_redirects})",
        )
    except httpx.HTTPError as exc:
        return BinaryFetchResult(
            ok=False,
            requested_url=url,
            expected_kind=expected_kind,
            final_url=current,
            redirect_chain=chain,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if owns_client:
            client.close()
