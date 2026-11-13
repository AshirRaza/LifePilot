"""LLM provider boundary — Anthropic Claude via langchain-anthropic."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from lifepilot.agent.config import AgentConfig, get_agent_config
from lifepilot.agent.errors import (
    AgentExecutionError,
    LLMNotConfiguredError,
)

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"


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
    """Text completion payload."""

    content: str
    provider: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StructuredLLMResult:
    """Structured completion payload."""

    value: BaseModel
    provider: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0


@runtime_checkable
class LLMProvider(Protocol):
    """Provider interface for text and structured generation."""

    provider_id: str
    model_id: str

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.0,
    ) -> LLMResult: ...

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[TModel],
        max_tokens: int,
        temperature: float = 0.0,
    ) -> StructuredLLMResult: ...


class DisabledLLMProvider:
    """Default provider: refuses calls until Anthropic is configured."""

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

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float = 0.0,
    ) -> StructuredLLMResult:
        raise LLMNotConfiguredError()


def _usage_from_message(message: Any) -> LLMUsage:
    meta = getattr(message, "usage_metadata", None) or {}
    if isinstance(meta, dict) and meta:
        inp = meta.get("input_tokens")
        out = meta.get("output_tokens")
        total = meta.get("total_tokens")
        if total is None and inp is not None and out is not None:
            total = int(inp) + int(out)
        return LLMUsage(
            input_tokens=int(inp) if inp is not None else None,
            output_tokens=int(out) if out is not None else None,
            total_tokens=int(total) if total is not None else None,
        )
    resp_meta = getattr(message, "response_metadata", None) or {}
    usage = resp_meta.get("usage") if isinstance(resp_meta, dict) else None
    if isinstance(usage, dict):
        inp = usage.get("input_tokens")
        out = usage.get("output_tokens")
        total = None
        if inp is not None and out is not None:
            total = int(inp) + int(out)
        return LLMUsage(
            input_tokens=int(inp) if inp is not None else None,
            output_tokens=int(out) if out is not None else None,
            total_tokens=total,
        )
    return LLMUsage()


def _is_retryable(exc: BaseException) -> bool:
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    markers = ("rate", "timeout", "temporar", "overload", "529", "429", "503", "500")
    return any(m in name or m in text for m in markers)


class AnthropicLLMProvider:
    """Anthropic Claude adapter (langchain-anthropic). No tools / browsing."""

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str,
        timeout_seconds: float = 45.0,
        max_retries: int = 2,
        max_concurrency: int = 2,
    ) -> None:
        if not api_key or not api_key.strip():
            raise LLMNotConfiguredError("ANTHROPIC_API_KEY is missing")
        if not model_id or not model_id.strip():
            raise LLMNotConfiguredError("LIFEPILOT_LLM_MODEL is missing")

        # Lazy import so unit tests/imports work without the package in weird envs.
        from langchain_anthropic import ChatAnthropic

        self.provider_id = "anthropic"
        self.model_id = model_id.strip()
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = max(0, int(max_retries))
        self._semaphore = threading.Semaphore(max(1, int(max_concurrency)))
        # Sonnet 5.5 rejects sampling params (temperature/top_p/top_k) and
        # forced tool_choice. Prefer json_schema structured output + effort.
        client_kwargs: dict[str, Any] = {
            "model": self.model_id,
            "api_key": api_key.strip(),
            "timeout": self._timeout_seconds,
            "max_retries": 0,  # we apply bounded retries ourselves
            "output_config": {"effort": "medium"},
        }
        # Avoid up-front thinking cost for single-shot Q&A (no tools).
        if "sonnet-5-5" in self.model_id or self.model_id.endswith("5-5"):
            client_kwargs["thinking"] = {"type": "between_tools"}
        self._client = ChatAnthropic(**client_kwargs)

    def _to_lc_messages(self, messages: list[LLMMessage]) -> list[Any]:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        out: list[Any] = []
        for msg in messages:
            role = (msg.role or "").strip().lower()
            if role == "system":
                out.append(SystemMessage(content=msg.content))
            elif role == "assistant":
                out.append(AIMessage(content=msg.content))
            else:
                out.append(HumanMessage(content=msg.content))
        return out

    def _run_with_retries(self, fn):  # type: ignore[no-untyped-def]
        attempt = 0
        while True:
            try:
                with self._semaphore:
                    return fn()
            except Exception as exc:  # noqa: BLE001
                if attempt >= self._max_retries or not _is_retryable(exc):
                    # Never include credentials; class name + short message only.
                    raise AgentExecutionError(
                        f"anthropic_call_failed:{exc.__class__.__name__}",
                        code="llm_provider_error",
                    ) from exc
                delay = min(4.0, 0.5 * (2**attempt))
                logger.warning(
                    "anthropic.retry attempt=%s error=%s",
                    attempt + 1,
                    exc.__class__.__name__,
                )
                time.sleep(delay)
                attempt += 1

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.0,
    ) -> LLMResult:
        lc_messages = self._to_lc_messages(messages)

        def _invoke():
            # Do not pass temperature — rejected by Claude Sonnet 5.5.
            _ = temperature
            return self._client.invoke(
                lc_messages,
                max_tokens=max_tokens,
            )

        started = time.perf_counter()
        message = self._run_with_retries(_invoke)
        _ = (time.perf_counter() - started) * 1000
        content = message.content if isinstance(message.content, str) else str(message.content)
        return LLMResult(
            content=content,
            provider=self.provider_id,
            model=self.model_id,
            usage=_usage_from_message(message),
            raw={"response_metadata": getattr(message, "response_metadata", {})},
        )

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[TModel],
        max_tokens: int,
        temperature: float = 0.0,
    ) -> StructuredLLMResult:
        lc_messages = self._to_lc_messages(messages)
        _ = temperature  # unused; Sonnet 5.5 rejects sampling parameters
        # Native json_schema — avoid function_calling (forced tool_choice → 400 on 5.5).
        structured = self._client.with_structured_output(
            schema,
            include_raw=True,
            method="json_schema",
        )

        def _invoke():
            return structured.invoke(
                lc_messages,
                max_tokens=max_tokens,
            )

        started = time.perf_counter()
        result = self._run_with_retries(_invoke)
        latency_ms = (time.perf_counter() - started) * 1000

        if not isinstance(result, dict):
            raise AgentExecutionError(
                "anthropic_invalid_structured_payload",
                code="llm_invalid_output",
            )
        parsed = result.get("parsed")
        raw_msg = result.get("raw")
        err = result.get("parsing_error")
        if err is not None and parsed is None:
            raise AgentExecutionError(
                f"anthropic_structured_parse_failed:{err.__class__.__name__}",
                code="llm_invalid_output",
            )
        if not isinstance(parsed, BaseModel):
            raise AgentExecutionError(
                "anthropic_structured_output_missing",
                code="llm_invalid_output",
            )
        usage = _usage_from_message(raw_msg) if raw_msg is not None else LLMUsage()
        return StructuredLLMResult(
            value=parsed,
            provider=self.provider_id,
            model=self.model_id,
            usage=usage,
            raw={"response_metadata": getattr(raw_msg, "response_metadata", {})},
            latency_ms=round(latency_ms, 3),
        )


_provider_override: LLMProvider | None = None
_provider_singleton: LLMProvider | None = None
_provider_lock = threading.Lock()


def set_llm_provider_override(provider: LLMProvider | None) -> None:
    """Tests only: inject a fake provider."""
    global _provider_override
    _provider_override = provider


def reset_llm_provider_override() -> None:
    set_llm_provider_override(None)


def reset_llm_provider_cache() -> None:
    """Drop cached live provider (tests / config reload)."""
    global _provider_singleton
    with _provider_lock:
        _provider_singleton = None


def anthropic_api_key_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def get_llm_provider(config: AgentConfig | None = None) -> LLMProvider:
    """Resolve provider from env. Never calls the network at import time."""
    if _provider_override is not None:
        return _provider_override

    cfg = config or get_agent_config()
    provider_name = (cfg.llm_provider or "").strip().lower()
    model_id = (cfg.llm_model or "").strip() or DEFAULT_ANTHROPIC_MODEL

    if provider_name != "anthropic":
        return DisabledLLMProvider(
            provider_id=provider_name or "disabled",
            model_id=model_id if provider_name else "none",
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return DisabledLLMProvider(provider_id="anthropic", model_id=model_id)

    global _provider_singleton
    with _provider_lock:
        if _provider_singleton is None:
            _provider_singleton = AnthropicLLMProvider(
                api_key=api_key,
                model_id=model_id,
                timeout_seconds=cfg.llm_timeout_seconds,
                max_retries=cfg.llm_max_retries,
                max_concurrency=cfg.max_llm_concurrency,
            )
        return _provider_singleton
