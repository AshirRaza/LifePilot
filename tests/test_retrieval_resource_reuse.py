"""Shared embedder / orchestrator reuse, isolation, and retrieval equivalence."""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.agent import (
    AgentConfig,
    AgentRequest,
    AgentStatus,
    EvidenceOrchestrator,
    get_evidence_orchestrator,
    reset_default_evidence_orchestrator,
    run_agent,
)
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
from lifepilot.rag.embedder import (
    FakeEmbedder,
    get_shared_embedder,
    reset_shared_embedder,
    set_shared_embedder,
)
from lifepilot.rag.retrieval import (
    InternalKnowledgeRetriever,
    InternalQuery,
    RetrievalStatus,
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
        heading_path="Eligibility",
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


@pytest.fixture(autouse=True)
def _reset_latency_state(reset_agent_runtime):
    yield


def test_shared_embedder_reused_across_retrievers(db_session: Session):
    fake = FakeEmbedder()
    set_shared_embedder(fake)
    r1 = InternalKnowledgeRetriever(db_session)
    r2 = InternalKnowledgeRetriever(db_session)
    assert r1.embedder is fake
    assert r2.embedder is fake
    assert get_shared_embedder() is fake


def test_default_orchestrator_reuses_embedder_instance():
    fake = FakeEmbedder()
    set_shared_embedder(fake)
    orch_a = get_evidence_orchestrator()
    orch_b = get_evidence_orchestrator()
    assert orch_a is orch_b
    assert orch_a._resolve_embedder() is fake
    assert orch_b._resolve_embedder() is fake


def test_repeated_requests_equivalent_ranked_evidence(db_session: Session):
    fake = FakeEmbedder()
    set_shared_embedder(fake)
    text = (
        "Skilled Worker visa applicants need a Certificate of Sponsorship "
        "and must meet the relevant salary threshold for their job code."
    )
    chunk = _seed_chunk(db_session, code="GB", text_value=text, url_suffix="sw-lat")
    _embed_chunk(db_session, chunk, fake)

    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(db_session),
        embedder=fake,
        config=AgentConfig(),
    )
    results = [
        orch.gather(
            question="Skilled Worker Certificate of Sponsorship salary",
            jurisdiction="GB",
            retrieval_mode="hybrid",
            top_k=5,
            include_external=False,
        )
        for _ in range(3)
    ]
    assert all(r.status == AgentStatus.SUCCESS for r in results)
    ids0 = [e.evidence_id for e in results[0].evidence]
    chunks0 = [e.chunk_id for e in results[0].evidence]
    for r in results[1:]:
        assert [e.evidence_id for e in r.evidence] == ids0
        assert [e.chunk_id for e in r.evidence] == chunks0


def test_direct_retriever_and_orchestrator_equivalence(db_session: Session):
    fake = FakeEmbedder()
    set_shared_embedder(fake)
    text = (
        "You must have a job offer from an approved UK sponsor to apply for "
        "the Skilled Worker visa under the immigration rules."
    )
    chunk = _seed_chunk(db_session, code="GB", text_value=text, url_suffix="eq")
    _embed_chunk(db_session, chunk, fake)

    query = InternalQuery(
        query="Skilled Worker approved UK sponsor job offer",
        jurisdiction="GB",
        mode="hybrid",
        top_k=5,
        pathway_id=None,
        diversify_documents=True,
        document_aware_passages=True,
        expand_adjacent_chunks=True,
    )
    direct = InternalKnowledgeRetriever(db_session, embedder=fake).retrieve(query)
    assert direct.status == RetrievalStatus.OK

    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(db_session),
        embedder=fake,
        config=AgentConfig(),
    )
    bundled = orch.gather(
        question=query.query,
        jurisdiction="GB",
        retrieval_mode="hybrid",
        top_k=5,
        include_external=False,
    )
    assert bundled.status == AgentStatus.SUCCESS
    assert [e.chunk_id for e in bundled.evidence] == [p.chunk_id for p in direct.passages]
    assert [e.document_id for e in bundled.evidence] == [
        p.document_id for p in direct.passages
    ]


def test_concurrent_requests_no_state_leakage(db_session: Session):
    fake = FakeEmbedder()
    set_shared_embedder(fake)
    gb_text = (
        "United Kingdom Skilled Worker substantive guidance for salary and "
        "sponsorship requirements under Home Office rules."
    )
    ca_text = (
        "Canada Express Entry substantive guidance for federal skilled worker "
        "points and permanent residence eligibility."
    )
    gb = _seed_chunk(db_session, code="GB", text_value=gb_text, url_suffix="gb-c")
    ca = _seed_chunk(db_session, code="CA", text_value=ca_text, url_suffix="ca-c")
    _embed_chunk(db_session, gb, fake)
    _embed_chunk(db_session, ca, fake)

    lock = threading.Lock()
    outcomes: dict[str, list[str]] = {}

    def _run(jur: str, question: str) -> None:
        orch = EvidenceOrchestrator(
            session_factory=_BoundSessionFactory(db_session),
            embedder=fake,
            config=AgentConfig(),
        )
        result = orch.gather(
            question=question,
            jurisdiction=jur,
            retrieval_mode="hybrid",
            top_k=5,
            include_external=False,
        )
        with lock:
            outcomes[jur] = [e.jurisdiction for e in result.evidence]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [
            pool.submit(_run, "GB", "United Kingdom Skilled Worker salary sponsorship"),
            pool.submit(_run, "CA", "Canada Express Entry permanent residence"),
        ]
        for fut in as_completed(futs):
            fut.result()

    assert outcomes["GB"]
    assert outcomes["CA"]
    assert all(j == "GB" for j in outcomes["GB"])
    assert all(j == "CA" for j in outcomes["CA"])


def test_current_version_still_filtered_with_shared_embedder(db_session: Session):
    fake = FakeEmbedder()
    set_shared_embedder(fake)
    current_text = (
        "Current Skilled Worker salary threshold guidance that must surface "
        "from the live document version only."
    )
    stale_text = (
        "Obsolete Skilled Worker salary figures that must never appear when "
        "is_current_version is false."
    )
    current = _seed_chunk(
        db_session, code="GB", text_value=current_text, url_suffix="cur", is_current=True
    )
    stale = _seed_chunk(
        db_session, code="GB", text_value=stale_text, url_suffix="old", is_current=False
    )
    _embed_chunk(db_session, current, fake)
    _embed_chunk(db_session, stale, fake)

    orch = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(db_session),
        embedder=fake,
        config=AgentConfig(),
    )
    result = orch.gather(
        question="Skilled Worker salary threshold guidance",
        jurisdiction="GB",
        include_external=False,
    )
    joined = " ".join(e.passage_text for e in result.evidence).lower()
    assert "obsolete" not in joined
    if result.evidence:
        assert all(e.chunk_id != stale.id for e in result.evidence)


def test_run_agent_repeated_requests_stable_with_fake_shared_embedder(db_session: Session):
    """Graph path reuses process orchestrator + shared embedder without leakage."""
    fake = FakeEmbedder()
    set_shared_embedder(fake)
    text = (
        "Graduate visa lets you stay in the UK after successful completion of "
        "an eligible course for a limited period."
    )
    chunk = _seed_chunk(db_session, code="GB", text_value=text, url_suffix="grad")
    _embed_chunk(db_session, chunk, fake)

    # Point default orchestrator at the test session + fake embedder.
    reset_default_evidence_orchestrator()
    from lifepilot.agent import orchestration as orch_mod

    orch_mod._default_orchestrator = EvidenceOrchestrator(
        session_factory=_BoundSessionFactory(db_session),
        embedder=fake,
        config=AgentConfig(),
    )

    responses = [
        run_agent(
            AgentRequest(
                question="Graduate visa stay after eligible course",
                jurisdiction="GB",
                retrieval={"mode": "hybrid", "top_k": 5, "include_external": False},
            )
        )
        for _ in range(3)
    ]
    assert all(r.status == AgentStatus.SUCCESS for r in responses)
    first_ids = [e.evidence_id for e in responses[0].evidence]
    for r in responses[1:]:
        assert [e.evidence_id for e in r.evidence] == first_ids
        assert r.request_id != responses[0].request_id
