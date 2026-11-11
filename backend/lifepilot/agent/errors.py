"""Agent error types."""

from __future__ import annotations


class AgentError(Exception):
    """Base class for agent foundation errors."""

    def __init__(self, message: str, *, code: str = "agent_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class AgentValidationError(AgentError):
    """Request or configuration failed validation."""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(message, code=code)


class AgentUnsupportedError(AgentError):
    """Requested operation is not supported yet or by policy."""

    def __init__(self, message: str, *, code: str = "unsupported_operation") -> None:
        super().__init__(message, code=code)


class AgentExecutionError(AgentError):
    """Workflow execution failed."""

    def __init__(self, message: str, *, code: str = "execution_failure") -> None:
        super().__init__(message, code=code)


class LLMNotConfiguredError(AgentError):
    """LLM provider/model is not configured or deliberately disabled."""

    def __init__(
        self,
        message: str = (
            "LLM provider is not configured. Answer generation is deferred; "
            "set LIFEPILOT_LLM_PROVIDER and LIFEPILOT_LLM_MODEL before enabling "
            "model calls."
        ),
        *,
        code: str = "llm_not_configured",
    ) -> None:
        super().__init__(message, code=code)
