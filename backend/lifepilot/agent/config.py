"""Agent configuration and execution limits (Phase 9A)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


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


def _env_str(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip()


@dataclass(frozen=True)
class AgentConfig:
    """Runtime limits and future LLM settings.

    Timeout / max-token fields are defined for later phases. Phase 9A enforces
    validation limits that already apply (question length, evidence cap,
    workflow steps). Request and connector timeouts are not yet wired into a
    live orchestrator.
    """

    max_question_length: int = 4000
    max_evidence_items: int = 20
    max_workflow_steps: int = 16
    request_timeout_seconds: float = 60.0
    external_timeout_seconds: float = 30.0
    max_llm_output_tokens: int = 2048
    llm_provider: str = ""
    llm_model: str = ""
    default_retrieval_top_k: int = 8
    max_retrieval_top_k: int = 20

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_provider and self.llm_model)


@lru_cache(maxsize=1)
def get_agent_config() -> AgentConfig:
    """Load agent config from environment (cached per process)."""
    return AgentConfig(
        max_question_length=_env_int("LIFEPILOT_AGENT_MAX_QUESTION_LENGTH", 4000),
        max_evidence_items=_env_int("LIFEPILOT_AGENT_MAX_EVIDENCE_ITEMS", 20),
        max_workflow_steps=_env_int("LIFEPILOT_AGENT_MAX_WORKFLOW_STEPS", 16),
        request_timeout_seconds=_env_float(
            "LIFEPILOT_AGENT_REQUEST_TIMEOUT_SECONDS", 60.0
        ),
        external_timeout_seconds=_env_float(
            "LIFEPILOT_AGENT_EXTERNAL_TIMEOUT_SECONDS", 30.0
        ),
        max_llm_output_tokens=_env_int("LIFEPILOT_AGENT_MAX_LLM_OUTPUT_TOKENS", 2048),
        llm_provider=_env_str("LIFEPILOT_LLM_PROVIDER", ""),
        llm_model=_env_str("LIFEPILOT_LLM_MODEL", ""),
        default_retrieval_top_k=_env_int("LIFEPILOT_AGENT_DEFAULT_TOP_K", 8),
        max_retrieval_top_k=_env_int("LIFEPILOT_AGENT_MAX_TOP_K", 20),
    )


def reset_agent_config_cache() -> None:
    """Clear cached config (tests only)."""
    get_agent_config.cache_clear()
