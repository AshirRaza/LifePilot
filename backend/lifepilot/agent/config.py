"""Agent configuration and execution limits."""

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
    """Runtime limits and LLM settings."""

    max_question_length: int = 4000
    max_evidence_items: int = 20
    max_workflow_steps: int = 16
    request_timeout_seconds: float = 90.0
    external_timeout_seconds: float = 30.0
    max_external_concurrency: int = 3
    max_llm_output_tokens: int = 2048
    llm_provider: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 45.0
    llm_max_retries: int = 2
    max_llm_concurrency: int = 2
    llm_max_evidence_items: int = 12
    llm_max_chars_per_passage: int = 1200
    llm_max_context_chars: int = 14000
    # Optional explicit USD pricing per million tokens (0 = disabled).
    llm_input_cost_per_mtok: float = 0.0
    llm_output_cost_per_mtok: float = 0.0
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
            "LIFEPILOT_AGENT_REQUEST_TIMEOUT_SECONDS", 90.0
        ),
        external_timeout_seconds=_env_float(
            "LIFEPILOT_AGENT_EXTERNAL_TIMEOUT_SECONDS", 30.0
        ),
        max_external_concurrency=max(
            1, _env_int("LIFEPILOT_AGENT_MAX_EXTERNAL_CONCURRENCY", 3)
        ),
        max_llm_output_tokens=_env_int("LIFEPILOT_AGENT_MAX_LLM_OUTPUT_TOKENS", 2048),
        llm_provider=_env_str("LIFEPILOT_LLM_PROVIDER", ""),
        llm_model=_env_str("LIFEPILOT_LLM_MODEL", ""),
        llm_timeout_seconds=_env_float("LIFEPILOT_LLM_TIMEOUT_SECONDS", 45.0),
        llm_max_retries=max(0, _env_int("LIFEPILOT_LLM_MAX_RETRIES", 2)),
        max_llm_concurrency=max(1, _env_int("LIFEPILOT_LLM_MAX_CONCURRENCY", 2)),
        llm_max_evidence_items=_env_int("LIFEPILOT_LLM_MAX_EVIDENCE_ITEMS", 12),
        llm_max_chars_per_passage=_env_int("LIFEPILOT_LLM_MAX_CHARS_PER_PASSAGE", 1200),
        llm_max_context_chars=_env_int("LIFEPILOT_LLM_MAX_CONTEXT_CHARS", 14000),
        llm_input_cost_per_mtok=_env_float("LIFEPILOT_LLM_INPUT_COST_PER_MTOK", 0.0),
        llm_output_cost_per_mtok=_env_float("LIFEPILOT_LLM_OUTPUT_COST_PER_MTOK", 0.0),
        default_retrieval_top_k=_env_int("LIFEPILOT_AGENT_DEFAULT_TOP_K", 8),
        max_retrieval_top_k=_env_int("LIFEPILOT_AGENT_MAX_TOP_K", 20),
    )


def reset_agent_config_cache() -> None:
    """Clear cached config (tests only)."""
    get_agent_config.cache_clear()
