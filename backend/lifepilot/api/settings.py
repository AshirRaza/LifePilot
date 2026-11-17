"""API deployment settings loaded from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return int(str(raw).strip())


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return float(str(raw).strip())


@dataclass(frozen=True)
class ApiSettings:
    """Internal/private backend defaults — fail closed for auth in production."""

    environment: str = "development"
    require_auth: bool = False
    api_keys: tuple[str, ...] = ()
    rate_limit_per_minute: int = 30
    max_concurrent_answers: int = 4
    max_request_bytes: int = 32_768
    include_evidence_in_response: bool = False
    log_questions: bool = False

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"production", "prod"}


@lru_cache(maxsize=1)
def get_api_settings() -> ApiSettings:
    env = (os.environ.get("LIFEPILOT_ENV") or "development").strip().lower()
    raw_keys = (os.environ.get("LIFEPILOT_API_KEYS") or "").strip()
    keys = tuple(k.strip() for k in raw_keys.split(",") if k.strip())
    # Production fails closed: auth required. Dev may run without keys for local use.
    default_require = env in {"production", "prod"}
    require_auth = _env_bool("LIFEPILOT_API_REQUIRE_AUTH", default_require)
    if require_auth and not keys and env in {"production", "prod"}:
        # Keep settings constructible for tests; app startup validates separately.
        pass
    return ApiSettings(
        environment=env or "development",
        require_auth=require_auth,
        api_keys=keys,
        rate_limit_per_minute=max(1, _env_int("LIFEPILOT_API_RATE_LIMIT_PER_MINUTE", 30)),
        max_concurrent_answers=max(1, _env_int("LIFEPILOT_API_MAX_CONCURRENT_ANSWERS", 4)),
        max_request_bytes=max(1024, _env_int("LIFEPILOT_API_MAX_REQUEST_BYTES", 32_768)),
        include_evidence_in_response=_env_bool(
            "LIFEPILOT_API_INCLUDE_EVIDENCE", False
        ),
        log_questions=_env_bool("LIFEPILOT_API_LOG_QUESTIONS", False),
    )


def reset_api_settings_cache() -> None:
    get_api_settings.cache_clear()


def assert_production_secrets(settings: ApiSettings | None = None) -> None:
    """Raise if production is missing required secrets."""
    cfg = settings or get_api_settings()
    if not cfg.is_production:
        return
    missing: list[str] = []
    if cfg.require_auth and not cfg.api_keys:
        missing.append("LIFEPILOT_API_KEYS")
    if not (os.environ.get("DATABASE_URL") or "").strip():
        missing.append("DATABASE_URL")
    if (os.environ.get("LIFEPILOT_LLM_PROVIDER") or "").strip().lower() == "anthropic":
        if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
            missing.append("ANTHROPIC_API_KEY")
    if missing:
        raise RuntimeError(
            "Production startup blocked; missing required configuration: "
            + ", ".join(missing)
        )
