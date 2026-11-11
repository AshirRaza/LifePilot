"""Phase 9A agent foundation tests — no network or API keys required."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from lifepilot.agent import (
    AgentRequest,
    AgentStatus,
    DisabledLLMProvider,
    EvidenceItem,
    EvidenceSourceType,
    EvidenceVerificationState,
    GRAPH_NODE_ORDER,
    LLMNotConfiguredError,
    build_agent_graph,
    build_evidence_id,
    evidence_from_external_item,
    evidence_from_internal_passage,
    get_agent_config,
    get_compiled_agent_graph,
    get_llm_provider,
    run_agent,
    validate_agent_request,
)
from lifepilot.agent.config import reset_agent_config_cache
from lifepilot.agent.errors import AgentValidationError
from lifepilot.agent.graph import reset_compiled_agent_graph
from lifepilot.agent.state import CHECKPOINTING_BOUNDARY_NOTE, initial_state_from_request


@pytest.fixture(autouse=True)
def _reset_agent_caches():
    reset_agent_config_cache()
    reset_compiled_agent_graph()
    yield
    reset_agent_config_cache()
    reset_compiled_agent_graph()


def test_request_requires_jurisdiction_and_question():
    with pytest.raises(ValidationError):
        AgentRequest(question="What documents do I need?", jurisdiction="")
    with pytest.raises(ValidationError):
        AgentRequest(question="   ", jurisdiction="GB")
    req = AgentRequest(
        question="Skilled Worker visa eligibility?",
        jurisdiction="gb",
        pathway_id="gb_skilled_worker",
    )
    assert req.jurisdiction == "GB"
    assert req.pathway_id == "gb_skilled_worker"


def test_request_question_length_limit(monkeypatch):
    monkeypatch.setenv("LIFEPILOT_AGENT_MAX_QUESTION_LENGTH", "20")
    reset_agent_config_cache()
    req = AgentRequest(question="x" * 25, jurisdiction="GB")
    with pytest.raises(AgentValidationError) as exc:
        validate_agent_request(req)
    assert exc.value.code == "question_too_long"


def test_config_defaults_and_env_overrides(monkeypatch):
    monkeypatch.delenv("LIFEPILOT_AGENT_MAX_EVIDENCE_ITEMS", raising=False)
    monkeypatch.delenv("LIFEPILOT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LIFEPILOT_LLM_MODEL", raising=False)
    reset_agent_config_cache()
    cfg = get_agent_config()
    assert cfg.max_evidence_items == 20
    assert cfg.max_workflow_steps == 16
    assert cfg.llm_configured is False

    monkeypatch.setenv("LIFEPILOT_AGENT_MAX_EVIDENCE_ITEMS", "7")
    monkeypatch.setenv("LIFEPILOT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("LIFEPILOT_LLM_MODEL", "placeholder-model")
    reset_agent_config_cache()
    cfg2 = get_agent_config()
    assert cfg2.max_evidence_items == 7
    assert cfg2.llm_configured is True


def test_evidence_verification_states_preserved():
    internal = evidence_from_internal_passage(
        jurisdiction="GB",
        official_url="https://www.gov.uk/skilled-worker-visa",
        document_id=160,
        document_version_id=1,
        chunk_id=13646,
        content_text="A Skilled Worker visa allows you to come to the UK.",
        score=0.91,
    )
    assert internal.source_type == EvidenceSourceType.INTERNAL_RAG
    assert (
        internal.verification_state
        == EvidenceVerificationState.UNVERIFIED_RETRIEVAL
    )
    assert "COMPLETE" in internal.applicability_limitations

    external = evidence_from_external_item(
        jurisdiction="GB",
        official_url="https://www.gov.uk/api/content/skilled-worker-visa",
        provider="govuk_content",
        passage_text="External guidance excerpt about Skilled Worker.",
        document_id="ext-1",
        title="Skilled Worker visa",
    )
    assert external.verification_state == EvidenceVerificationState.UNVERIFIED_EXTERNAL
    assert external.evidence_id.startswith("ev_")

    with pytest.raises(ValidationError):
        EvidenceItem(
            evidence_id="ev_badpairing000000000001",
            source_type=EvidenceSourceType.INTERNAL_RAG,
            jurisdiction="GB",
            passage_text="text",
            verification_state=EvidenceVerificationState.UNVERIFIED_EXTERNAL,
        )


def test_evidence_ids_are_stable():
    a = build_evidence_id(
        source_type=EvidenceSourceType.INTERNAL_RAG,
        jurisdiction="GB",
        source_url="https://example.test/a",
        document_id=1,
        document_version_id=2,
        chunk_id=3,
        passage_text="same",
    )
    b = build_evidence_id(
        source_type=EvidenceSourceType.INTERNAL_RAG,
        jurisdiction="GB",
        source_url="https://example.test/a",
        document_id=1,
        document_version_id=2,
        chunk_id=3,
        passage_text="same",
    )
    c = build_evidence_id(
        source_type=EvidenceSourceType.INTERNAL_RAG,
        jurisdiction="GB",
        source_url="https://example.test/a",
        document_id=1,
        document_version_id=2,
        chunk_id=3,
        passage_text="different",
    )
    assert a == b
    assert a != c


def test_llm_provider_disabled_without_keys():
    provider = get_llm_provider()
    assert isinstance(provider, DisabledLLMProvider)
    with pytest.raises(LLMNotConfiguredError):
        provider.complete([], max_tokens=16)


def test_graph_compiles_and_runs_deterministically():
    graph = get_compiled_agent_graph()
    assert graph is not None
    assert GRAPH_NODE_ORDER[0] == "validate_initialize"
    assert GRAPH_NODE_ORDER[-1] == "finalize"

    state = initial_state_from_request(
        request_id="req_test_1",
        question="What are Skilled Worker eligibility requirements?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        user_circumstances={"nationality": "example"},
        conversation_id="conv_a",
        retrieval_mode="hybrid",
        retrieval_top_k=8,
        include_external=False,
        max_workflow_steps=16,
    )
    out1 = graph.invoke(state)
    out2 = graph.invoke(state)
    assert out1["status"] == AgentStatus.SUCCESS.value
    assert out2["status"] == AgentStatus.SUCCESS.value
    assert out1["step_count"] == out2["step_count"] == len(GRAPH_NODE_ORDER)
    assert out1["answer"] is None
    assert out1["evidence"] == []
    assert "evidence_orchestration" in out1["deferred_stages"]
    assert "answer_generation" in out1["deferred_stages"]
    assert any("pathway_id is a user-provided hint" in w for w in out1["warnings"])


def test_run_agent_invalid_and_success_statuses():
    bad = run_agent({"question": "hello", "jurisdiction": ""})
    assert bad.status == AgentStatus.INVALID_REQUEST
    assert bad.errors

    ok = run_agent(
        AgentRequest(
            question="Student visa financial requirement?",
            jurisdiction="GB",
            conversation_id="conv_iso_1",
        )
    )
    assert ok.status == AgentStatus.SUCCESS
    assert ok.answer is None
    assert ok.evidence == []
    assert "retrieved_evidence" in ok.missing_information
    assert ok.jurisdiction == "GB"
    assert ok.timing.total_ms >= 0


def test_state_isolation_between_requests():
    r1 = run_agent(
        AgentRequest(
            question="Question one about Skilled Worker",
            jurisdiction="GB",
            conversation_id="conv_1",
            user_circumstances={"note": "user-a"},
        )
    )
    r2 = run_agent(
        AgentRequest(
            question="Question two about Employment Pass",
            jurisdiction="SG",
            conversation_id="conv_2",
            user_circumstances={"note": "user-b"},
        )
    )
    assert r1.request_id != r2.request_id
    assert r1.conversation_id == "conv_1"
    assert r2.conversation_id == "conv_2"
    assert r1.jurisdiction == "GB"
    assert r2.jurisdiction == "SG"


def test_build_graph_without_checkpointer_and_no_api_keys(monkeypatch):
    # Ensure missing credentials do not block import/compilation.
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "LIFEPILOT_LLM_PROVIDER",
        "LIFEPILOT_LLM_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_agent_config_cache()
    reset_compiled_agent_graph()
    built = build_agent_graph()
    compiled = built.compile()
    assert compiled is not None
    assert "PostgreSQL" in CHECKPOINTING_BOUNDARY_NOTE
    # Confirm process still has no forced provider key requirement.
    assert os.environ.get("OPENAI_API_KEY") is None
