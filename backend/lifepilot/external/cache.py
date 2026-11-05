"""Simple TTL response cache for external API GETs (no Redis)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class CacheEntry:
    stored_at: float
    ttl_seconds: float
    payload: Any

    def expired(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (now - self.stored_at) > self.ttl_seconds


class ResponseCache:
    """In-memory cache with optional on-disk persistence under ``data/cache/``."""

    def __init__(
        self,
        *,
        default_ttl_seconds: float = 3600.0,
        disk_dir: Path | None = None,
        max_entries: int = 512,
    ) -> None:
        self.default_ttl_seconds = default_ttl_seconds
        self.disk_dir = disk_dir
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._mem: dict[str, CacheEntry] = {}
        if self.disk_dir is not None:
            self.disk_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_key(method: str, url: str, params: dict[str, Any] | None = None) -> str:
        raw = json.dumps(
            {"m": method.upper(), "u": url, "p": params or {}},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._mem.get(key)
            if entry is not None:
                if entry.expired():
                    del self._mem[key]
                else:
                    return entry.payload
            if self.disk_dir is None:
                return None
            path = self.disk_dir / f"{key}.json"
            if not path.is_file():
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entry = CacheEntry(
                    stored_at=float(data["stored_at"]),
                    ttl_seconds=float(data["ttl_seconds"]),
                    payload=data["payload"],
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                return None
            if entry.expired():
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
            self._mem[key] = entry
            return entry.payload

    def set(
        self,
        key: str,
        payload: Any,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        entry = CacheEntry(stored_at=time.time(), ttl_seconds=ttl, payload=payload)
        with self._lock:
            if len(self._mem) >= self.max_entries:
                # Drop oldest expired first, else arbitrary first key.
                expired = [k for k, v in self._mem.items() if v.expired()]
                for k in expired[: max(1, len(expired))]:
                    self._mem.pop(k, None)
                while len(self._mem) >= self.max_entries:
                    self._mem.pop(next(iter(self._mem)))
            self._mem[key] = entry
            if self.disk_dir is not None:
                path = self.disk_dir / f"{key}.json"
                try:
                    path.write_text(
                        json.dumps(
                            {
                                "stored_at": entry.stored_at,
                                "ttl_seconds": entry.ttl_seconds,
                                "payload": payload,
                            },
                            default=str,
                        ),
                        encoding="utf-8",
                    )
                except OSError:
                    pass

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._mem.pop(key, None)
            if self.disk_dir is not None:
                path = self.disk_dir / f"{key}.json"
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear(self) -> None:
        with self._lock:
            self._mem.clear()
            if self.disk_dir is not None and self.disk_dir.is_dir():
                for path in self.disk_dir.glob("*.json"):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
