"""LifePilot immigration agent foundation (Phase 9A).

Provides typed contracts, evidence models, execution limits, an LLM provider
boundary, and a minimal LangGraph workflow. Live evidence orchestration and
answer generation are deferred.
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
    "EvidenceSourceType",
    "EvidenceVerificationState",
    "GRAPH_NODE_ORDER",
    "LLMMessage",
    "LLMNotConfiguredError",
    "LLMProvider",
    "LLMResult",
    "RetrievalOptions",
    "TimingMetadata",
    "build_agent_graph",
    "build_evidence_id",
    "evidence_from_external_item",
    "evidence_from_internal_passage",
    "get_agent_config",
    "get_compiled_agent_graph",
    "get_llm_provider",
    "run_agent",
    "validate_agent_request",
]
