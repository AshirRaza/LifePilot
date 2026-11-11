"""LifePilot immigration agent.

Provides typed contracts, evidence models, execution limits, an LLM provider
boundary, and a LangGraph workflow that orchestrates internal hybrid RAG plus
optional external knowledge connectors. Answer generation remains deferred.
"""

from lifepilot.agent.config import AgentConfig, get_agent_config
from lifepilot.agent.contracts import (
    AgentRequest,
    AgentResponse,
    AgentStatus,
    RetrievalOptions,
    TimingMetadata,
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
    DisabledLLMProvider,
    LLMMessage,
    LLMProvider,
    LLMResult,
    get_llm_provider,
)
from lifepilot.agent.runner import run_agent
from lifepilot.agent.state import CHECKPOINTING_BOUNDARY_NOTE, AgentGraphState

__all__ = [
    "AgentConfig",
    "AgentError",
    "AgentExecutionError",
    "AgentGraphState",
    "AgentRequest",
    "AgentResponse",
    "AgentStatus",
    "AgentUnsupportedError",
    "AgentValidationError",
    "CHECKPOINTING_BOUNDARY_NOTE",
    "DisabledLLMProvider",
    "EvidenceItem",
    "EvidenceOrchestrator",
    "EvidenceSourceType",
    "EvidenceVerificationState",
    "GRAPH_NODE_ORDER",
    "LLMMessage",
    "LLMNotConfiguredError",
    "LLMProvider",
    "LLMResult",
    "OrchestrationResult",
    "RetrievalOptions",
    "TimingMetadata",
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
    "run_agent",
    "set_evidence_orchestrator_override",
    "validate_agent_request",
]
