"""Redirect-safe HTML fetch for a single approved seed URL."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin

import certifi
import httpx

from lifepilot.crawl.policy import CrawlPolicy
from lifepilot.crawl.robots import check_robots
from lifepilot.registry.urls import url_in_any_allowed_pattern

_HTML_TYPES = {"text/html", "application/xhtml+xml"}


@dataclass
class HtmlFetchResult:
    ok: bool
    requested_url: str
    final_url: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    body: bytes | None = None
    redirect_chain: list[str] = field(default_factory=list)
    error: str | None = None


def _content_type_base(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(";", 1)[0].strip().lower()


def _is_html_content_type(content_type: str | None) -> bool:
    base = _content_type_base(content_type)
    return base in _HTML_TYPES if base else False


def _looks_like_pdf(body_prefix: bytes) -> bool:
    return body_prefix.lstrip().startswith(b"%PDF")


def fetch_html_seed(
    seed_url: str,
    *,
    allowed_patterns: list[str],
    policy: CrawlPolicy,
    client: httpx.Client | None = None,
) -> HtmlFetchResult:
    """GET ``seed_url`` as HTML, validating each redirect against allowlist + robots."""
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=policy.request_timeout_seconds,
            follow_redirects=False,
            verify=certifi.where(),
            headers={
                "User-Agent": policy.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            },
        )

    current = seed_url
    chain: list[str] = []
    try:
        for _ in range(policy.max_redirects + 1):
            if not url_in_any_allowed_pattern(current, allowed_patterns):
                return HtmlFetchResult(
                    ok=False,
                    requested_url=seed_url,
                    final_url=current,
                    redirect_chain=chain,
                    error="URL outside approved allowlist scope",
                )

            robots = check_robots(current, policy, client=client)
            if policy.require_robots_ok and not robots.fetched:
                return HtmlFetchResult(
                    ok=False,
                    requested_url=seed_url,
                    final_url=current,
                    redirect_chain=chain,
                    error=f"robots.txt unavailable: {robots.error}",
                )
            if robots.can_fetch is False:
                return HtmlFetchResult(
                    ok=False,
                    requested_url=seed_url,
                    final_url=current,
                    redirect_chain=chain,
                    error="robots.txt disallows fetch for this URL",
                )

            with client.stream("GET", current) as response:
                status = response.status_code
                # Redirects: validate next hop without reading a body.
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    response.close()
                    if not location:
                        return HtmlFetchResult(
                            ok=False,
                            requested_url=seed_url,
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
                    return HtmlFetchResult(
                        ok=False,
                        requested_url=seed_url,
                        final_url=current,
                        status_code=status,
                        redirect_chain=chain,
                        error=f"HTTP {status}",
                    )

                content_type = response.headers.get("content-type")
                if not _is_html_content_type(content_type):
                    response.close()
                    return HtmlFetchResult(
                        ok=False,
                        requested_url=seed_url,
                        final_url=current,
                        status_code=status,
                        content_type=content_type,
                        redirect_chain=chain,
                        error=f"non-HTML content-type: {content_type!r}",
                    )

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > policy.max_response_bytes:
                        response.close()
                        return HtmlFetchResult(
                            ok=False,
                            requested_url=seed_url,
                            final_url=current,
                            status_code=status,
                            content_type=content_type,
                            redirect_chain=chain,
                            error=(
                                f"response exceeds max_response_bytes="
                                f"{policy.max_response_bytes}"
                            ),
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)

            if _looks_like_pdf(body[:16]):
                return HtmlFetchResult(
                    ok=False,
                    requested_url=seed_url,
                    final_url=current,
                    status_code=status,
                    content_type=content_type,
                    redirect_chain=chain,
                    error="body looks like PDF; HTML only in this phase",
                )

            return HtmlFetchResult(
                ok=True,
                requested_url=seed_url,
                final_url=current,
                status_code=status,
                content_type=content_type,
                body=body,
                redirect_chain=chain,
            )

        return HtmlFetchResult(
            ok=False,
            requested_url=seed_url,
            final_url=current,
            redirect_chain=chain,
            error=f"too many redirects (max={policy.max_redirects})",
        )
    except httpx.HTTPError as exc:
        return HtmlFetchResult(
            ok=False,
            requested_url=seed_url,
            final_url=current,
            redirect_chain=chain,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if owns_client:
            client.close()
