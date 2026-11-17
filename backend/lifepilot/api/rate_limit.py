"""Simple in-process rate limiting and answer concurrency caps."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from typing import Iterator

from fastapi import HTTPException, Request, status

from lifepilot.api.settings import get_api_settings


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, *, limit: int, window_seconds: float = 60.0) -> None:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            while bucket and (now - bucket[0]) > window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded",
                )
            bucket.append(now)


class ConcurrencyGate:
    def __init__(self, limit: int) -> None:
        self._sema = threading.BoundedSemaphore(value=max(1, limit))

    @contextmanager
    def acquire(self, *, timeout: float = 0.0) -> Iterator[None]:
        ok = self._sema.acquire(timeout=timeout if timeout > 0 else None)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Server is at answer concurrency capacity",
            )
        try:
            yield
        finally:
            self._sema.release()


_rate_limiter = SlidingWindowRateLimiter()
_gates: dict[int, ConcurrencyGate] = {}
_gates_lock = threading.Lock()


def reset_rate_limit_state() -> None:
    """Tests only: clear in-memory limiter buckets and concurrency gates."""
    with _rate_limiter._lock:
        _rate_limiter._hits.clear()
    with _gates_lock:
        _gates.clear()


def client_key(request: Request, principal: str | None) -> str:
    if principal:
        return f"principal:{principal}"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    if request.client and request.client.host:
        return f"ip:{request.client.host}"
    return "ip:unknown"


def enforce_rate_limit(request: Request, principal: str | None) -> None:
    cfg = get_api_settings()
    _rate_limiter.check(
        client_key(request, principal),
        limit=cfg.rate_limit_per_minute,
        window_seconds=60.0,
    )


def answer_concurrency_gate() -> ConcurrencyGate:
    cfg = get_api_settings()
    limit = cfg.max_concurrent_answers
    with _gates_lock:
        gate = _gates.get(limit)
        if gate is None:
            gate = ConcurrencyGate(limit)
            _gates[limit] = gate
        return gate
