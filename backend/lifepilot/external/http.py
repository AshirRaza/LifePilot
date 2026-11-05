"""Shared HTTP client for documented public APIs (read-only).

This client is intentionally separate from the crawl allowlist fetcher:
it only targets owner-configured public API base URLs for discovery.
It never writes to the source registry or document store.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import certifi
import httpx

from lifepilot.crawl.http_fetch import parse_retry_after_seconds
from lifepilot.external.cache import ResponseCache
from lifepilot.external.models import RetrievalStatus

logger = logging.getLogger(__name__)

EXTERNAL_USER_AGENT = (
    "LifePilotExternal/0.1 "
    "(+https://github.com/AshirRaza/LifePilot; "
    "read-only public API discovery; does not ingest documents)"
)


@dataclass(frozen=True)
class HttpClientConfig:
    timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_base_seconds: float = 0.5
    max_response_bytes: int = 5_000_000
    max_redirects: int = 5
    cache_ttl_seconds: float = 3600.0
    respect_retry_after: bool = True


@dataclass
class HttpResponse:
    ok: bool
    status_code: int | None
    url: str
    final_url: str | None
    json_data: Any | None = None
    text: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    retrieval_status: RetrievalStatus = RetrievalStatus.OK
    from_cache: bool = False


class ExternalHttpClient:
    """httpx wrapper with timeouts, bounded retries, rate-limit handling, cache."""

    def __init__(
        self,
        *,
        config: HttpClientConfig | None = None,
        cache: ResponseCache | None = None,
        client: httpx.Client | None = None,
        allowed_hosts: frozenset[str] | None = None,
    ) -> None:
        self.config = config or HttpClientConfig()
        self.cache = cache
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=self.config.timeout_seconds,
            follow_redirects=True,
            max_redirects=self.config.max_redirects,
            verify=certifi.where(),
            headers={
                "User-Agent": EXTERNAL_USER_AGENT,
                "Accept": "application/json, application/ld+json, text/plain, */*;q=0.1",
            },
        )
        self.allowed_hosts = allowed_hosts

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ExternalHttpClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _host_allowed(self, url: str) -> bool:
        if self.allowed_hosts is None:
            return True
        host = (urlparse(url).hostname or "").lower()
        return host in self.allowed_hosts

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
        cache_ttl: float | None = None,
    ) -> HttpResponse:
        return self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            use_cache=use_cache,
            cache_ttl=cache_ttl,
            expect_json=True,
        )

    def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
        cache_ttl: float | None = None,
    ) -> HttpResponse:
        return self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            use_cache=use_cache,
            cache_ttl=cache_ttl,
            expect_json=False,
        )

    def post_form(
        self,
        url: str,
        *,
        data: dict[str, Any],
        headers: dict[str, str] | None = None,
        use_cache: bool = False,
    ) -> HttpResponse:
        return self._request(
            "POST",
            url,
            data=data,
            headers=headers,
            use_cache=use_cache,
            expect_json=True,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
        cache_ttl: float | None = None,
        expect_json: bool = True,
    ) -> HttpResponse:
        if not self._host_allowed(url):
            return HttpResponse(
                ok=False,
                status_code=None,
                url=url,
                final_url=None,
                error=f"Host not in allowed API hosts for this connector: {urlparse(url).hostname}",
                retrieval_status=RetrievalStatus.BLOCKED,
            )

        cache_key = None
        if use_cache and self.cache is not None and method.upper() == "GET":
            cache_key = ResponseCache.make_key(method, url, params)
            cached = self.cache.get(cache_key)
            if cached is not None:
                return HttpResponse(
                    ok=True,
                    status_code=200,
                    url=url,
                    final_url=cached.get("final_url", url),
                    json_data=cached.get("json_data"),
                    text=cached.get("text"),
                    headers=cached.get("headers") or {},
                    from_cache=True,
                    retrieval_status=RetrievalStatus.OK,
                )

        last_error: str | None = None
        last_status: RetrievalStatus = RetrievalStatus.TRANSPORT_ERROR
        last_code: int | None = None

        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=headers,
                )
                last_code = response.status_code
                body = response.content
                if len(body) > self.config.max_response_bytes:
                    return HttpResponse(
                        ok=False,
                        status_code=response.status_code,
                        url=url,
                        final_url=str(response.url),
                        error=(
                            f"Response exceeded max_response_bytes="
                            f"{self.config.max_response_bytes}"
                        ),
                        retrieval_status=RetrievalStatus.INVALID_RESPONSE,
                    )

                if response.status_code == 429:
                    last_status = RetrievalStatus.RATE_LIMITED
                    last_error = "HTTP 429 Too Many Requests"
                    retry_after = parse_retry_after_seconds(
                        response.headers.get("Retry-After")
                    )
                    delay = (
                        retry_after
                        if retry_after is not None and self.config.respect_retry_after
                        else self.config.backoff_base_seconds * (2**attempt)
                    )
                    if attempt < self.config.max_retries:
                        time.sleep(min(delay, 30.0))
                        continue
                    return HttpResponse(
                        ok=False,
                        status_code=429,
                        url=url,
                        final_url=str(response.url),
                        error=last_error,
                        retrieval_status=RetrievalStatus.RATE_LIMITED,
                        headers=dict(response.headers),
                    )

                if response.status_code >= 500:
                    last_status = RetrievalStatus.TRANSPORT_ERROR
                    last_error = f"HTTP {response.status_code}"
                    if attempt < self.config.max_retries:
                        time.sleep(self.config.backoff_base_seconds * (2**attempt))
                        continue
                    return HttpResponse(
                        ok=False,
                        status_code=response.status_code,
                        url=url,
                        final_url=str(response.url),
                        error=last_error,
                        retrieval_status=last_status,
                    )

                if response.status_code >= 400:
                    return HttpResponse(
                        ok=False,
                        status_code=response.status_code,
                        url=url,
                        final_url=str(response.url),
                        error=f"HTTP {response.status_code}",
                        retrieval_status=(
                            RetrievalStatus.NO_RESULTS
                            if response.status_code in (404, 410)
                            else RetrievalStatus.TRANSPORT_ERROR
                        ),
                        text=response.text[:2000] if response.text else None,
                    )

                json_data = None
                text_body = None
                if expect_json:
                    try:
                        json_data = response.json()
                    except ValueError:
                        return HttpResponse(
                            ok=False,
                            status_code=response.status_code,
                            url=url,
                            final_url=str(response.url),
                            error="Malformed JSON response",
                            retrieval_status=RetrievalStatus.INVALID_RESPONSE,
                            text=response.text[:2000] if response.text else None,
                        )
                else:
                    text_body = response.text

                if cache_key is not None and self.cache is not None:
                    self.cache.set(
                        cache_key,
                        {
                            "final_url": str(response.url),
                            "json_data": json_data,
                            "text": text_body,
                            "headers": {
                                k: v
                                for k, v in response.headers.items()
                                if k.lower()
                                not in {"set-cookie", "authorization", "cookie"}
                            },
                        },
                        ttl_seconds=cache_ttl or self.config.cache_ttl_seconds,
                    )

                logger.info(
                    "external_http method=%s host=%s status=%s cache=%s",
                    method,
                    urlparse(str(response.url)).hostname,
                    response.status_code,
                    False,
                )
                return HttpResponse(
                    ok=True,
                    status_code=response.status_code,
                    url=url,
                    final_url=str(response.url),
                    json_data=json_data,
                    text=text_body,
                    headers=dict(response.headers),
                    retrieval_status=RetrievalStatus.OK,
                )
            except httpx.TimeoutException as exc:
                last_status = RetrievalStatus.TIMEOUT
                last_error = f"Timeout: {exc}"
                if attempt < self.config.max_retries:
                    time.sleep(self.config.backoff_base_seconds * (2**attempt))
                    continue
            except httpx.HTTPError as exc:
                last_status = RetrievalStatus.TRANSPORT_ERROR
                last_error = f"HTTP error: {exc}"
                if attempt < self.config.max_retries:
                    time.sleep(self.config.backoff_base_seconds * (2**attempt))
                    continue

        return HttpResponse(
            ok=False,
            status_code=last_code,
            url=url,
            final_url=None,
            error=last_error,
            retrieval_status=last_status,
        )
