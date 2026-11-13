"""Evidence orchestration tests — internal RAG + external connectors."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.agent import (
    AgentConfig,
    AgentRequest,
    AgentStatus,
    EvidenceOrchestrator,
    EvidenceSourceType,
    EvidenceVerificationState,
    OrchestrationResult,
    explicit_providers_for_jurisdiction,
    run_agent,
    set_evidence_orchestrator_override,
)
from lifepilot.agent.config import reset_agent_config_cache
from lifepilot.agent.evidence import evidence_from_internal_passage
from lifepilot.db.models import (
    PLANNED_EMBEDDING_MODEL_ID,
    Authority,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.external.cache import ResponseCache
from lifepilot.external.http import ExternalHttpClient, HttpClientConfig
from lifepilot.external.models import (
    ProviderId,
    ProviderOutcome,
    RetrievalStatus as ExternalRetrievalStatus,
)
from lifepilot.external.router import ExternalKnowledgeRouter
from lifepilot.rag.embedder import FakeEmbedder
from lifepilot.rag.retrieval import (
    InternalKnowledgeRetriever,
    InternalQuery,
    RetrievalResult,
    RetrievalStatus as InternalRetrievalStatus,
    RetrievedPassage,
)


def _sha(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


@contextmanager
def _session_cm(session: Session):
    yield session


class _BoundSessionFactory:
    def __init__(self, session: Session) -> None:
        self._session = session

    def __call__(self):
        return _session_cm(self._session)


def _seed_chunk(
    session: Session,
    *,
    code: str,
    text_value: str,
    heading: str | None = "Eligibility",
    url_suffix: str = "page",
    is_current: bool = True,
) -> DocumentChunk:
    jur = session.scalar(select(Jurisdiction).where(Jurisdiction.code == code))
    if jur is None:
        jur = Jurisdiction(
            code=code,
            name=f"Country {code}",
            jurisdiction_type="supranational" if code == "EU" else "country",
            country_code=None if code == "EU" else code,
        )
        session.add(jur)
        session.flush()
    auth = Authority(jurisdiction_id=jur.id, name=f"Auth {code}-{url_suffix}")
    session.add(auth)
    session.flush()
    source = Source(
        authority_id=auth.id,
        jurisdiction_id=jur.id,
        name=f"Source {code}-{url_suffix}",
        seed_url=f"https://example.test/{code.lower()}/{url_suffix}-seed",
        seed_url_normalized=f"https://example.test/{code.lower()}/{url_suffix}-seed",
        review_status="approved",
    )
    session.add(source)
    session.flush()
    doc = Document(
        authority_id=auth.id,
        primary_source_id=source.id,
        canonical_url=f"https://example.test/{code.lower()}/{url_suffix}",
        url_normalized=f"https://example.test/{code.lower()}/{url_suffix}",
        content_kind="html_page",
        language="en",
        discovery_status="seed",
    )
    session.add(doc)
    session.flush()
    ver = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=source.id,
        version_number=1 if is_current else 0,
        source_url=doc.canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime.now(timezone.utc),
        http_status=200,
        content_sha256=_sha(f"raw-{code}-{url_suffix}-{is_current}"),
        byte_size=10,
        storage_path=f"raw/documents/{doc.id}/v{'1' if is_current else '0'}.html",
        is_current=is_current,
    )
    session.add(ver)
    session.flush()
    chunk = DocumentChunk(
        document_id=doc.id,
        document_version_id=ver.id,
        source_id=source.id,
        jurisdiction_id=jur.id,
        jurisdiction_code=code,
        chunk_index=0,
        content_text=text_value,
        content_sha256=_sha(text_value),
        is_current_version=is_current,
        heading_path=heading,
    )
    session.add(chunk)
    session.flush()
    return chunk


def _embed_chunk(session: Session, chunk: DocumentChunk, embedder: FakeEmbedder) -> None:
    vec = embedder.embed_passages([chunk.content_text]).vectors[0]
    session.add(
        DocumentChunkEmbedding(
            chunk_id=chunk.id,
            embedding_model_id=PLANNED_EMBEDDING_MODEL_ID,
            embedding=list(map(float, vec.tolist())),
        )
    )
    session.flush()


def _passage(
    *,
    jurisdiction: str = "GB",
    text: str = "A Skilled Worker visa lets you work in the UK for an approved sponsor.",
    chunk_id: int = 1,
    document_id: int = 10,
    document_version_id: int = 20,
    score: float = 0.9,
) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=chunk_id,
        document_id=document_id,
        document_version_id=document_version_id,
        source_id=1,
        jurisdiction_code=jurisdiction,
        official_url=f"https://example.test/{jurisdiction.lower()}/guide",
        heading_path="Eligibility",
        content_text=text,
        score=score,
    )


def _mock_retriever(passages: list[RetrievedPassage], status=InternalRetrievalStatus.OK):
    retriever = MagicMock(spec=InternalKnowledgeRetriever)
    retriever.retrieve.return_value = RetrievalResult(
        status=status,
        query="q",
        jurisdiction="GB",
        mode="hybrid",
        passages=passages,
    )
    return retriever


def _govuk_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "/api/content/" in url:
        return httpx.Response(
            200,
            json={
                "base_path": "/skilled-worker-visa",
                "content_id": "abc-123",
                "title": "Skilled Worker visa",
                "description": "Work in the UK",
                "document_type": "guide",
                "schema_name": "guide",
                "locale": "en",
                "first_published_at": "2020-01-01T00:00:00.000Z",
                "public_updated_at": "2024-01-01T00:00:00.000Z",
                "details": {
                    "body": (
                        "<p>You can apply for a Skilled Worker visa if you have "
                        "a job offer from a licensed sponsor and meet the skill "
                        "and salary requirements published on GOV.UK.</p>"
                    )
                },
            },
            request=request,
            headers={"Content-Type": "application/json"},
        )
    return httpx.Response(404, json={"error": "missing"}, request=request)


def _external_router_with_handler(handler) -> ExternalKnowledgeRouter:
    transport = httpx.MockTransport(handler)
    raw = httpx.Client(transport=transport, follow_redirects=True)
    http = ExternalHttpClient(
        config=HttpClientConfig(
            timeout_seconds=5.0, max_retries=1, backoff_base_seconds=0.01
        ),
        cache=ResponseCache(default_ttl_seconds=60.0, disk_dir=None),
        client=raw,
        allowed_hosts=None,
    )
    return ExternalKnowledgeRouter(http=http, enable_disk_cache=False)


@pytest.fixture(autouse=True)
def _reset_agent_state(reset_agent_runtime):
    yield


# ---------------------------------------------------------------------------
# Provider routing helpers
# ---------------------------------------------------------------------------


def test_explicit_providers_no_all_provider_fallback():
    assert explicit_providers_for_jurisdiction("GB") == [ProviderId.GOVUK_CONTENT]
    assert ProviderId.CANADA_OPEN in explicit_providers_for_jurisdiction("CA")
    assert explicit_providers_for_jurisdiction("SG") == []
    assert explicit_providers_for_jurisdiction("AU") == []


# ---------------------------------------------------------------------------
# Internal hybrid retrieval integration
# ---------------------------------------------------------------------------


def test_internal_hybrid_retrieval_integration(db_session: Session):
    fake = FakeEmbedder()
    text = (
        "Skilled Worker visa applicants need a Certificate of Sponsorship "
        "and must meet the relevant salary threshold for their job."
    )
    chunk = _seed_chunk(db_session, code="GB", text_value=text, url_suffix="sw")
    _embed_chunk(db_session, chunk, fake)

    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(db_session),
        embedder=fake,
        config=AgentConfig(request_timeout_seconds=30.0),
    )
    result = orch.gather(
        question="Skilled Worker Certificate of Sponsorship salary",
        jurisdiction="GB",
        retrieval_mode="hybrid",
        top_k=5,
        include_external=False,
    )
    assert result.status == AgentStatus.SUCCESS
    assert result.evidence
    assert all(e.source_type == EvidenceSourceType.INTERNAL_RAG for e in result.evidence)
    assert all(
        e.verification_state == EvidenceVerificationState.UNVERIFIED_RETRIEVAL
        for e in result.evidence
    )
    assert all(e.document_id is not None and e.chunk_id is not None for e in result.evidence)
    assert result.internal_ms >= 0
    assert result.normalize_ms >= 0


def test_pathway_hint_not_passed_as_chunk_filter(db_session: Session):
    captured: list[InternalQuery] = []

    class CapturingRetriever(InternalKnowledgeRetriever):
        def retrieve(self, query: InternalQuery) -> RetrievalResult:  # type: ignore[override]
            captured.append(query)
            return RetrievalResult(
                status=InternalRetrievalStatus.NO_RESULTS,
                query=query.query,
                jurisdiction=query.jurisdiction,
                mode=query.mode,
                passages=[],
            )

    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(db_session),
        embedder=FakeEmbedder(),
        internal_retriever_factory=lambda session, embedder=None: CapturingRetriever(
            session, embedder=embedder
        ),
    )
    result = orch.gather(
        question="What are the requirements?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        include_external=False,
    )
    assert captured
    assert captured[0].pathway_id is None
    assert captured[0].mode == "hybrid"
    assert captured[0].document_aware_passages is True
    assert any("pathway_id was not used as a chunk filter" in w for w in result.warnings)


def test_jurisdiction_isolation_filters_foreign_passages():
    retriever = _mock_retriever(
        [
            _passage(jurisdiction="GB", text="UK skilled worker guidance passage here."),
            _passage(
                jurisdiction="CA",
                text="Canadian permanent residence guidance passage here.",
                chunk_id=2,
            ),
        ]
    )
    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(MagicMock()),
        internal_retriever_factory=lambda session, embedder=None: retriever,
        config=AgentConfig(),
    )
    # Bypass real DB by patching _retrieve_internal.
    orch._retrieve_internal = lambda **kwargs: (  # type: ignore[method-assign]
        [
            _passage(jurisdiction="GB", text="UK skilled worker guidance passage here."),
            _passage(
                jurisdiction="CA",
                text="Canadian permanent residence guidance passage here.",
                chunk_id=2,
            ),
        ],
        "ok",
        1.0,
    )
    result = orch.gather(
        question="visa requirements",
        jurisdiction="GB",
        include_external=False,
    )
    assert result.evidence
    assert all(e.jurisdiction == "GB" for e in result.evidence)


def test_eu_separation_blocks_eu_external_on_gb_request():
    calls: list[ProviderId] = []

    class RecordingOrch(EvidenceOrchestrator):
        def _run_provider(self, router, provider_id, query):  # type: ignore[override]
            calls.append(provider_id)
            return ProviderOutcome(
                provider=provider_id,
                status=ExternalRetrievalStatus.NO_RESULTS,
                items=[],
            )

    router = _external_router_with_handler(_govuk_handler)
    orch = RecordingOrch(
        session_factory=_BoundSessionFactory(MagicMock()),
        external_router=router,
        config=AgentConfig(),
    )
    orch._retrieve_internal = lambda **kwargs: ([], "no_results", 0.5)  # type: ignore[method-assign]
    result = orch.gather(
        question="Skilled Worker visa",
        jurisdiction="GB",
        include_external=True,
    )
    assert ProviderId.GOVUK_CONTENT in calls
    assert ProviderId.EU_OPENDATA not in calls
    assert ProviderId.EURLEX_CELLAR not in calls
    assert ProviderId.EU_IMMIGRATION_PORTAL not in calls
    assert result.status == AgentStatus.INSUFFICIENT_EVIDENCE


def test_current_version_filtering(db_session: Session):
    fake = FakeEmbedder()
    current_text = (
        "Current version: Skilled Worker salary thresholds were updated this year "
        "and applicants must check the official GOV.UK table."
    )
    stale_text = (
        "Stale version: obsolete Skilled Worker salary figures that must not appear "
        "in retrieval results for current guidance."
    )
    current = _seed_chunk(
        db_session, code="GB", text_value=current_text, url_suffix="current", is_current=True
    )
    stale = _seed_chunk(
        db_session, code="GB", text_value=stale_text, url_suffix="stale", is_current=False
    )
    _embed_chunk(db_session, current, fake)
    _embed_chunk(db_session, stale, fake)

    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(db_session),
        embedder=fake,
    )
    result = orch.gather(
        question="Skilled Worker salary thresholds GOV.UK",
        jurisdiction="GB",
        retrieval_mode="hybrid",
        include_external=False,
    )
    joined = " ".join(e.passage_text for e in result.evidence)
    assert "obsolete" not in joined.lower()
    if result.evidence:
        assert all(e.document_version_id == current.document_version_id for e in result.evidence)


# ---------------------------------------------------------------------------
# External routing / failures
# ---------------------------------------------------------------------------


def test_external_provider_routing_govuk():
    router = _external_router_with_handler(_govuk_handler)
    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(MagicMock()),
        external_router=router,
        config=AgentConfig(),
    )
    orch._retrieve_internal = lambda **kwargs: ([], "no_results", 1.0)  # type: ignore[method-assign]
    result = orch.gather(
        question="Skilled Worker visa requirements",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        include_external=True,
    )
    assert result.external_provider_outcomes
    assert any(o["provider"] == "govuk_content" for o in result.external_provider_outcomes)
    if result.evidence:
        assert all(
            e.verification_state == EvidenceVerificationState.UNVERIFIED_EXTERNAL
            for e in result.evidence
            if e.source_type == EvidenceSourceType.EXTERNAL_API
        )


def test_unsupported_external_jurisdiction_skips_providers():
    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(MagicMock()),
        external_router=_external_router_with_handler(_govuk_handler),
        config=AgentConfig(),
    )
    orch._retrieve_internal = lambda **kwargs: ([], "no_results", 0.2)  # type: ignore[method-assign]
    result = orch.gather(
        question="Employment Pass requirements",
        jurisdiction="SG",
        include_external=True,
    )
    assert result.external_provider_outcomes == []
    assert any("No mapped external providers" in w for w in result.warnings)
    assert result.status == AgentStatus.INSUFFICIENT_EVIDENCE


def test_empty_retrieval_results_insufficient():
    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(MagicMock()),
        config=AgentConfig(),
    )
    orch._retrieve_internal = lambda **kwargs: ([], "no_results", 0.1)  # type: ignore[method-assign]
    result = orch.gather(
        question="unknown topic with no corpus match",
        jurisdiction="GB",
        include_external=False,
    )
    assert result.status == AgentStatus.INSUFFICIENT_EVIDENCE
    assert result.evidence == []
    assert "retrieved_evidence" in result.missing_information


def test_provider_timeout_and_partial_source_success():
    internal = [
        _passage(
            text=(
                "Internal hybrid retrieval returned a substantive UK guidance "
                "passage about Skilled Worker sponsorship duties."
            )
        )
    ]

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=request)

    router = _external_router_with_handler(timeout_handler)
    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(MagicMock()),
        external_router=router,
        config=AgentConfig(external_timeout_seconds=1.0),
    )
    orch._retrieve_internal = lambda **kwargs: (internal, "ok", 2.0)  # type: ignore[method-assign]
    result = orch.gather(
        question="Skilled Worker sponsorship",
        jurisdiction="GB",
        include_external=True,
    )
    assert result.status == AgentStatus.SUCCESS
    assert result.evidence
    assert any(e.source_type == EvidenceSourceType.INTERNAL_RAG for e in result.evidence)
    assert any("partial_source:" in w for w in result.warnings)


def test_provider_hard_failure_without_evidence_is_failure_or_insufficient():
    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(MagicMock()),
        config=AgentConfig(),
    )

    def boom(**_kwargs):
        raise RuntimeError("db down")

    orch._retrieve_internal = boom  # type: ignore[method-assign]
    result = orch.gather(
        question="anything",
        jurisdiction="GB",
        include_external=False,
    )
    assert result.status == AgentStatus.EXECUTION_FAILURE
    assert result.evidence == []


# ---------------------------------------------------------------------------
# Normalization / limits / isolation
# ---------------------------------------------------------------------------


def test_evidence_normalization_stable_ids():
    a = evidence_from_internal_passage(
        jurisdiction="GB",
        official_url="https://www.gov.uk/skilled-worker-visa",
        document_id=160,
        document_version_id=1,
        chunk_id=13646,
        content_text="A Skilled Worker visa allows you to come to the UK to do an eligible job.",
        score=0.88,
    )
    b = evidence_from_internal_passage(
        jurisdiction="GB",
        official_url="https://www.gov.uk/skilled-worker-visa",
        document_id=160,
        document_version_id=1,
        chunk_id=13646,
        content_text="A Skilled Worker visa allows you to come to the UK to do an eligible job.",
        score=0.11,
    )
    assert a.evidence_id == b.evidence_id
    assert a.verification_state == EvidenceVerificationState.UNVERIFIED_RETRIEVAL


def test_request_limits_and_timeouts(monkeypatch):
    monkeypatch.setenv("LIFEPILOT_AGENT_REQUEST_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("LIFEPILOT_AGENT_MAX_EVIDENCE_ITEMS", "2")
    reset_agent_config_cache()

    slow_passages = [
        _passage(chunk_id=i, text=f"Substantive passage number {i} " + ("x" * 40))
        for i in range(5)
    ]

    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(MagicMock()),
        config=AgentConfig(request_timeout_seconds=0.01, max_evidence_items=2),
    )

    def slow_internal(**_kwargs):
        time.sleep(0.05)
        return slow_passages, "ok", 50.0

    orch._retrieve_internal = slow_internal  # type: ignore[method-assign]
    # Deadline already expired before gather work.
    result = orch.gather(
        question="timeout probe",
        jurisdiction="GB",
        include_external=False,
        deadline_monotonic=time.perf_counter() - 1.0,
    )
    assert result.status == AgentStatus.EXECUTION_FAILURE
    assert any("deadline" in e for e in result.errors)

    # Cap evidence items when retrieval succeeds.
    orch2 = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(MagicMock()),
        config=AgentConfig(max_evidence_items=2),
    )
    orch2._retrieve_internal = lambda **kwargs: (slow_passages, "ok", 1.0)  # type: ignore[method-assign]
    capped = orch2.gather(
        question="cap probe",
        jurisdiction="GB",
        top_k=50,
        include_external=False,
    )
    assert capped.status == AgentStatus.SUCCESS
    assert len(capped.evidence) <= 2


def test_concurrent_request_isolation():
    results: dict[str, OrchestrationResult] = {}
    lock = threading.Lock()

    def _run(jur: str, text: str) -> None:
        orch = EvidenceOrchestrator(
            session_factory=_BoundSessionFactory(MagicMock()),
            config=AgentConfig(),
        )
        orch._retrieve_internal = lambda **kwargs: (  # type: ignore[method-assign]
            [_passage(jurisdiction=jur, text=text, chunk_id=hash(jur) % 10_000)],
            "ok",
            1.0,
        )
        out = orch.gather(question=f"q-{jur}", jurisdiction=jur, include_external=False)
        with lock:
            results[jur] = out

    t1 = threading.Thread(
        target=_run,
        args=("GB", "United Kingdom skilled worker substantive guidance passage."),
    )
    t2 = threading.Thread(
        target=_run,
        args=("CA", "Canada open work permit substantive guidance passage here."),
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert results["GB"].evidence[0].jurisdiction == "GB"
    assert results["CA"].evidence[0].jurisdiction == "CA"
    assert "Canada" not in results["GB"].evidence[0].passage_text
    assert "United Kingdom" not in results["CA"].evidence[0].passage_text


def test_no_accidental_persistent_writes(db_session: Session):
    """External orchestration must not insert Source/Document rows."""
    before_source_count = len(list(db_session.scalars(select(Source)).all()))
    before_doc_count = len(list(db_session.scalars(select(Document)).all()))
    router = _external_router_with_handler(_govuk_handler)
    assert getattr(router.cache, "disk_dir", None) is None

    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(db_session),
        external_router=router,
        embedder=FakeEmbedder(),
        config=AgentConfig(),
    )
    orch.gather(
        question="Skilled Worker visa",
        jurisdiction="GB",
        include_external=True,
    )
    after_source_count = len(list(db_session.scalars(select(Source)).all()))
    after_doc_count = len(list(db_session.scalars(select(Document)).all()))
    assert after_source_count == before_source_count
    assert after_doc_count == before_doc_count


def test_run_agent_with_injected_orchestrator_end_to_end():
    from lifepilot.agent.providers import (
        DisabledLLMProvider,
        set_llm_provider_override,
    )

    class FakeOrch:
        def gather(self, **kwargs: Any) -> OrchestrationResult:
            item = evidence_from_internal_passage(
                jurisdiction="GB",
                official_url="https://example.test/gb/sw",
                document_id=1,
                document_version_id=1,
                chunk_id=99,
                content_text=(
                    "Injected substantive Skilled Worker evidence for graph wiring."
                ),
                score=0.77,
            )
            return OrchestrationResult(
                evidence=[item],
                status=AgentStatus.SUCCESS,
                warnings=["test-warning"],
                internal_ms=3.0,
                external_ms=0.0,
                normalize_ms=0.5,
                internal_status="ok",
            )

    # Evidence-wiring test only — never spend Anthropic calls here.
    set_llm_provider_override(DisabledLLMProvider())
    set_evidence_orchestrator_override(FakeOrch())  # type: ignore[arg-type]
    response = run_agent(
        AgentRequest(
            question="What are the UK Skilled Worker visa requirements?",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            retrieval={"mode": "hybrid", "top_k": 8, "include_external": False},
        )
    )
    assert response.status == AgentStatus.SUCCESS
    assert len(response.evidence) == 1
    assert response.evidence[0].chunk_id == 99
    assert response.answer is None
    assert response.timing.internal_retrieval_ms == 3.0
    assert any("test-warning" in w for w in response.warnings)
    assert any("pathway_id is a user-provided hint" in w for w in response.warnings)


@pytest.mark.skipif(
    not os.environ.get("LIFEPILOT_LIVE_EXTERNAL"),
    reason="Set LIFEPILOT_LIVE_EXTERNAL=1 to run live external smoke tests",
)
def test_live_evidence_smoke_optional():
    """Bounded live smoke using approved GOV.UK connector when enabled."""
    reset_evidence_orchestrator_override()
    orch = EvidenceOrchestrator(config=AgentConfig(request_timeout_seconds=45.0))
    try:
        # Internal may be empty on fresh envs; external GOV.UK should respond.
        result = orch.gather(
            question="Skilled Worker visa",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            include_external=True,
        )
    finally:
        orch.close()
    assert result.status in {
        AgentStatus.SUCCESS,
        AgentStatus.INSUFFICIENT_EVIDENCE,
    }
    assert result.external_ms >= 0
