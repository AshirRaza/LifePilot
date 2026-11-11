"""LLM provider boundary — interface only in Phase 9A (no network calls)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from lifepilot.agent.config import AgentConfig, get_agent_config
from lifepilot.agent.errors import LLMNotConfiguredError


@dataclass(frozen=True)
class LLMMessage:
    role: str
    content: str


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class LLMResult:
    """Future structured completion payload."""

    content: str
    provider: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal provider interface for later structured generation."""

    provider_id: str
    model_id: str

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.0,
    ) -> LLMResult: ...


class DisabledLLMProvider:
    """Default provider: refuses calls so Phase 9A never hits the network."""

    def __init__(
        self,
        *,
        provider_id: str = "",
        model_id: str = "",
    ) -> None:
        self.provider_id = provider_id or "disabled"
        self.model_id = model_id or "none"

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.0,
    ) -> LLMResult:
        raise LLMNotConfiguredError()


def get_llm_provider(config: AgentConfig | None = None) -> LLMProvider:
    """Resolve the configured provider.

    Phase 9A always returns ``DisabledLLMProvider``. When provider/model env
    vars are set they are recorded for later phases but still do not authorize
    live calls in this phase.
    """
    cfg = config or get_agent_config()
    return DisabledLLMProvider(
        provider_id=cfg.llm_provider or "disabled",
        model_id=cfg.llm_model or "none",
    )
