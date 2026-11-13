"""LifePilot immigration agent — evidence orchestration and grounded answers."""

from lifepilot.agent.answer import AnswerClaim, GroundedAnswer
from lifepilot.agent.config import AgentConfig, get_agent_config
from lifepilot.agent.contracts import (
    AgentRequest,
    AgentResponse,
    AgentStatus,
    RetrievalOptions,
    TimingMetadata,
    UsageMetadata,
    validate_agent_request,
)
from lifepilot.agent.errors import (
    AgentError,
    AgentExecutionError,
    AgentUnsupportedError,
    AgentValidationError,
    LLMNotConfiguredError,
)
from lifepilot.agent.evidence import (
    EvidenceItem,
    EvidenceSourceType,
    EvidenceVerificationState,
    build_evidence_id,
    evidence_from_external_item,
    evidence_from_internal_passage,
)
from lifepilot.agent.graph import (
    GRAPH_NODE_ORDER,
    build_agent_graph,
    get_compiled_agent_graph,
)
from lifepilot.agent.orchestration import (
    EvidenceOrchestrator,
    OrchestrationResult,
    explicit_providers_for_jurisdiction,
    get_evidence_orchestrator,
    reset_default_evidence_orchestrator,
    reset_evidence_orchestrator_override,
    set_evidence_orchestrator_override,
)
from lifepilot.agent.providers import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicLLMProvider,
    DisabledLLMProvider,
    LLMMessage,
    LLMProvider,
    LLMResult,
    LLMUsage,
    StructuredLLMResult,
    get_llm_provider,
    reset_llm_provider_cache,
    reset_llm_provider_override,
    set_llm_provider_override,
)
from lifepilot.agent.runner import run_agent
from lifepilot.agent.state import CHECKPOINTING_BOUNDARY_NOTE, AgentGraphState

__all__ = [
    "DEFAULT_ANTHROPIC_MODEL",
    "AgentConfig",
    "AgentError",
    "AgentExecutionError",
    "AgentGraphState",
    "AgentRequest",
    "AgentResponse",
    "AgentStatus",
    "AgentUnsupportedError",
    "AgentValidationError",
    "AnthropicLLMProvider",
    "AnswerClaim",
    "CHECKPOINTING_BOUNDARY_NOTE",
    "DisabledLLMProvider",
    "EvidenceItem",
    "EvidenceOrchestrator",
    "EvidenceSourceType",
    "EvidenceVerificationState",
    "GRAPH_NODE_ORDER",
    "GroundedAnswer",
    "LLMMessage",
    "LLMNotConfiguredError",
    "LLMProvider",
    "LLMResult",
    "LLMUsage",
    "OrchestrationResult",
    "RetrievalOptions",
    "StructuredLLMResult",
    "TimingMetadata",
    "UsageMetadata",
    "build_agent_graph",
    "build_evidence_id",
    "evidence_from_external_item",
    "evidence_from_internal_passage",
    "explicit_providers_for_jurisdiction",
    "get_agent_config",
    "get_compiled_agent_graph",
    "get_evidence_orchestrator",
    "get_llm_provider",
    "reset_default_evidence_orchestrator",
    "reset_evidence_orchestrator_override",
    "reset_llm_provider_cache",
    "reset_llm_provider_override",
    "run_agent",
    "set_evidence_orchestrator_override",
    "set_llm_provider_override",
    "validate_agent_request",
]
