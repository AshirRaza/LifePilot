"""Tests for Phase 8E internal hybrid retrieval and truncation safety."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

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
from lifepilot.rag.chunker import E5_SAFE_MAX_CHARS, chunk_structured_document
from lifepilot.rag.embedder import FakeEmbedder
from lifepilot.rag.retrieval import (
    InternalKnowledgeRetriever,
    InternalQuery,
    RetrievalStatus,
    diversify_by_document,
    reciprocal_rank_fusion,
    select_document_aware_passages,
)


def _sha(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


def _seed_chunk(
    session: Session,
    *,
    code: str,
    text_value: str,
    heading: str | None = "Eligibility",
    url_suffix: str = "page",
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
        version_number=1,
        source_url=doc.canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime.now(timezone.utc),
        http_status=200,
        content_sha256=_sha(f"raw-{code}-{url_suffix}"),
        byte_size=10,
        storage_path=f"raw/documents/{doc.id}/v1.html",
        is_current=True,
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
        is_current_version=True,
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


def test_rrf_deterministic_and_dedupes():
    fused = reciprocal_rank_fusion([[3, 1, 2], [2, 3, 4]], k=60)
    ids = [i for i, _ in fused]
    assert len(ids) == len(set(ids))
    assert ids[0] in (2, 3)
    # Stable tie-break by id when scores equal
    a = reciprocal_rank_fusion([[1, 2], [2, 1]], k=60)
    b = reciprocal_rank_fusion([[1, 2], [2, 1]], k=60)
    assert a == b


def test_rrf_ignores_empty_keyword_list():
    semantic = [10, 20, 30]
    fused = reciprocal_rank_fusion([semantic, []], k=60)
    assert [cid for cid, _ in fused] == semantic


def test_rrf_weights_prefer_semantic():
    semantic = [1, 2, 3]
    keyword = [99, 98, 97]
    unweighted = dict(reciprocal_rank_fusion([semantic, keyword], k=60))
    weighted = dict(
        reciprocal_rank_fusion([semantic, keyword], k=60, weights=[2.0, 1.0])
    )
    # Semantic weight increases the relative score of semantic rank-1 vs
    # keyword-only rank-1.
    assert weighted[1] / weighted[99] > unweighted[1] / unweighted[99]
    assert weighted[1] > weighted[99]


def test_diversify_by_document_prefers_distinct_docs():
    ranked = [11, 12, 21, 13, 31, 22]
    mapping = {11: 1, 12: 1, 13: 1, 21: 2, 22: 2, 31: 3}
    out = diversify_by_document(
        ranked, mapping, top_k=3, max_chunks_per_document=1
    )
    assert out == [11, 21, 31]
    # Extra slots fill with additional chunks from earlier docs.
    out2 = diversify_by_document(
        ranked, mapping, top_k=5, max_chunks_per_document=1
    )
    assert out2[:3] == [11, 21, 31]
    assert set(out2[3:]) <= {12, 13, 22}


def test_select_document_aware_passages_diversity_then_within_doc():
    document_ranks = [10, 20]
    per_doc = {
        10: [(101, 0.95), (102, 0.90), (103, 0.80)],
        20: [(201, 0.88), (202, 0.70)],
    }
    meta = {
        101: (0, "Eligibility"),
        102: (1, "Eligibility > Criteria"),
        103: (5, "Fees"),
        201: (0, "Overview"),
        202: (1, "Overview > Details"),
    }
    out = select_document_aware_passages(
        document_ranks,
        per_doc,
        meta,
        top_k=4,
        max_chunks_per_document=2,
        expand_adjacent=False,
    )
    # Depth-0 across boost docs first, then round-robin deeper passages.
    assert out[0] == 101
    assert out[1] == 201
    assert out[2] == 102
    assert out[3] == 202


def test_select_document_aware_expands_adjacent_same_section():
    document_ranks = [10]
    per_doc = {
        10: [(101, 0.95), (102, 0.93), (103, 0.50)],
    }
    meta = {
        101: (0, "Money you need"),
        102: (1, "Money you need > Financial requirement"),
        103: (8, "Other"),
    }
    out = select_document_aware_passages(
        document_ranks,
        per_doc,
        meta,
        top_k=3,
        max_chunks_per_document=1,
        expand_adjacent=True,
        adjacent_score_floor=0.92,
    )
    # Primary pick 101, adjacent 102 shares section and clears score floor.
    assert out[0] == 101
    assert 102 in out
    assert 103 not in out


def test_chunker_respects_e5_safe_max_chars():
    long_para = ("Salary thresholds and sponsorship rules. " * 80).strip()
    assert len(long_para) > E5_SAFE_MAX_CHARS
    drafts = chunk_structured_document(
        {
            "title": "Guidance",
            "blocks": [
                {"type": "heading", "level": 1, "text": "Eligibility"},
                {"type": "paragraph", "text": long_para},
            ],
        },
        min_chars=40,
    )
    assert drafts
    assert all(len(d.content_text) <= E5_SAFE_MAX_CHARS for d in drafts)
    joined = " ".join(d.content_text for d in drafts)
    assert "Salary thresholds" in joined


def test_retrieval_requires_jurisdiction(db_session: Session):
    retriever = InternalKnowledgeRetriever(db_session, embedder=FakeEmbedder())
    result = retriever.retrieve(
        InternalQuery(query="salary", jurisdiction="", mode="keyword")
    )
    assert result.status == RetrievalStatus.INVALID_REQUEST


def test_pathway_filter_unsupported_without_associations(db_session: Session):
    _seed_chunk(
        db_session,
        code="GB",
        text_value="Skilled Worker visa salary requirements from a licensed sponsor.",
        url_suffix="sw",
    )
    retriever = InternalKnowledgeRetriever(db_session, embedder=FakeEmbedder())
    result = retriever.retrieve(
        InternalQuery(
            query="salary",
            jurisdiction="GB",
            mode="keyword",
            pathway_id="gb_skilled_worker",
        )
    )
    assert result.status == RetrievalStatus.UNSUPPORTED_FILTER


def test_keyword_and_hybrid_jurisdiction_isolation(db_session: Session):
    gb = _seed_chunk(
        db_session,
        code="GB",
        text_value="Skilled Worker visa salary threshold and Certificate of Sponsorship.",
        url_suffix="gb1",
    )
    ie = _seed_chunk(
        db_session,
        code="IE",
        text_value="Critical Skills Employment Permit salary threshold requirements.",
        url_suffix="ie1",
    )
    eu = _seed_chunk(
        db_session,
        code="EU",
        text_value="EU Blue Card highly qualified employment salary conditions.",
        url_suffix="eu1",
    )
    fake = FakeEmbedder()
    for chunk in (gb, ie, eu):
        _embed_chunk(db_session, chunk, fake)

    # Ensure generated FTS column is populated (migration).
    db_session.execute(text("SELECT search_tsv FROM document_chunks LIMIT 1"))

    retriever = InternalKnowledgeRetriever(db_session, embedder=fake)
    kw = retriever.retrieve(
        InternalQuery(query="salary threshold", jurisdiction="GB", mode="keyword", top_k=5)
    )
    assert kw.status == RetrievalStatus.OK
    assert kw.passages
    assert all(p.jurisdiction_code == "GB" for p in kw.passages)
    assert all(p.verification_state == "unverified_retrieval" for p in kw.passages)
    assert all("not validated pathway evidence" in p.applicability_warning for p in kw.passages)

    hy = retriever.retrieve(
        InternalQuery(query="salary threshold", jurisdiction="GB", mode="hybrid", top_k=5)
    )
    assert hy.status == RetrievalStatus.OK
    assert all(p.jurisdiction_code == "GB" for p in hy.passages)

    eu_hit = retriever.retrieve(
        InternalQuery(query="Blue Card", jurisdiction="EU", mode="keyword", top_k=5)
    )
    assert eu_hit.status == RetrievalStatus.OK
    assert all(p.jurisdiction_code == "EU" for p in eu_hit.passages)


def test_semantic_current_version_only(db_session: Session):
    current = _seed_chunk(
        db_session,
        code="NL",
        text_value="Highly skilled migrant residence permit income requirement.",
        url_suffix="nl-cur",
    )
    stale = _seed_chunk(
        db_session,
        code="NL",
        text_value="Outdated highly skilled migrant guidance that must not appear.",
        url_suffix="nl-old",
    )
    stale.is_current_version = False
    db_session.flush()
    fake = FakeEmbedder()
    _embed_chunk(db_session, current, fake)
    _embed_chunk(db_session, stale, fake)

    retriever = InternalKnowledgeRetriever(db_session, embedder=fake)
    result = retriever.retrieve(
        InternalQuery(
            query="highly skilled migrant income",
            jurisdiction="NL",
            mode="semantic",
            top_k=5,
        )
    )
    assert result.status == RetrievalStatus.OK
    assert all(p.chunk_id != stale.id for p in result.passages)
    assert any(p.chunk_id == current.id for p in result.passages)


def test_empty_results(db_session: Session):
    retriever = InternalKnowledgeRetriever(db_session, embedder=FakeEmbedder())
    result = retriever.retrieve(
        InternalQuery(
            query="zzznomatchtokenxyz",
            jurisdiction="JP",
            mode="keyword",
            top_k=5,
        )
    )
    assert result.status == RetrievalStatus.NO_RESULTS
    assert result.passages == []


def test_hybrid_empty_keyword_falls_back_to_semantic(db_session: Session):
    """NL-style failure guard: empty FTS must not demote semantic gold."""
    gold = _seed_chunk(
        db_session,
        code="NL",
        text_value=(
            "Highly skilled migrant residence permit requirements for a "
            "recognised sponsor employer applying via the IND Business Portal."
        ),
        url_suffix="nl-hsm-gold",
    )
    distractor = _seed_chunk(
        db_session,
        code="NL",
        text_value="General residence permit overview for families and students.",
        url_suffix="nl-other",
    )
    fake = FakeEmbedder()
    _embed_chunk(db_session, gold, fake)
    _embed_chunk(db_session, distractor, fake)

    retriever = InternalKnowledgeRetriever(db_session, embedder=fake)
    retriever._semantic_search = (  # type: ignore[method-assign]
        lambda query, jurisdiction, limit: [(gold.id, 0.91), (distractor.id, 0.40)]
    )
    retriever._keyword_search = (  # type: ignore[method-assign]
        lambda query, jurisdiction, limit: []
    )
    hybrid = retriever.retrieve(
        InternalQuery(
            query="HSM IND recognised sponsor criteria",
            jurisdiction="NL",
            mode="hybrid",
            top_k=5,
            diversify_documents=False,
            document_aware_passages=False,
        )
    )
    assert hybrid.status == RetrievalStatus.OK
    assert [p.chunk_id for p in hybrid.passages][:2] == [gold.id, distractor.id]


def test_hybrid_filters_weak_keyword_only_noise(db_session: Session):
    gold = _seed_chunk(
        db_session,
        code="NL",
        text_value="Highly skilled migrant recognised sponsor IND salary criteria.",
        url_suffix="nl-gold2",
    )
    weak_a = _seed_chunk(
        db_session,
        code="NL",
        text_value="Unrelated tourist visa leaflet mentioning residence vaguely.",
        url_suffix="nl-weak-a",
    )
    weak_b = _seed_chunk(
        db_session,
        code="NL",
        text_value="Another weak keyword-only brochure about moving abroad.",
        url_suffix="nl-weak-b",
    )
    fake = FakeEmbedder()
    for chunk in (gold, weak_a, weak_b):
        _embed_chunk(db_session, chunk, fake)

    retriever = InternalKnowledgeRetriever(db_session, embedder=fake)
    # Many weak keyword-only hits that would otherwise flood unweighted RRF.
    weak_ids = [weak_a.id, weak_b.id] + [weak_a.id + 1000 + i for i in range(20)]
    retriever._semantic_search = (  # type: ignore[method-assign]
        lambda query, jurisdiction, limit: [(gold.id, 0.93)]
    )
    retriever._keyword_search = (  # type: ignore[method-assign]
        lambda query, jurisdiction, limit: [
            (cid, 0.02) for cid in weak_ids
        ]
    )
    # Filter unit: weak keyword-only ids are dropped when far below best score
    # and absent from semantic pool. Here best_kw==0.02 so floor keeps them;
    # force a strong keyword hit on an unrelated id then weak-only noise.
    retriever._keyword_search = (  # type: ignore[method-assign]
        lambda query, jurisdiction, limit: (
            [(weak_a.id, 1.0)]
            + [(cid, 0.05) for cid in weak_ids[1:]]
        )
    )
    filtered = retriever._filter_weak_keyword_ids(
        semantic_ids=[gold.id],
        keyword_hits=[(weak_a.id, 1.0), (weak_b.id, 0.05)],
    )
    assert filtered == [weak_a.id]

    hybrid = retriever.retrieve(
        InternalQuery(
            query="Highly skilled migrant recognised sponsor",
            jurisdiction="NL",
            mode="hybrid",
            top_k=5,
            diversify_documents=False,
            document_aware_passages=False,
        )
    )
    assert hybrid.status == RetrievalStatus.OK
    assert any(p.chunk_id == gold.id for p in hybrid.passages)


def test_document_diversification_limits_duplicates(db_session: Session):
    jur_code = "GB"
    base = _seed_chunk(
        db_session,
        code=jur_code,
        text_value="Skilled Worker visa eligibility licensed sponsor requirement one.",
        url_suffix="sw-a",
    )
    extras: list[DocumentChunk] = []
    for i, text_value in enumerate(
        [
            "Skilled Worker visa eligibility licensed sponsor requirement two.",
            "Skilled Worker visa eligibility licensed sponsor requirement three.",
        ],
        start=1,
    ):
        chunk = DocumentChunk(
            document_id=base.document_id,
            document_version_id=base.document_version_id,
            source_id=base.source_id,
            jurisdiction_id=base.jurisdiction_id,
            jurisdiction_code=jur_code,
            chunk_index=i,
            content_text=text_value,
            content_sha256=_sha(text_value),
            is_current_version=True,
            heading_path=f"Eligibility/{i}",
        )
        db_session.add(chunk)
        db_session.flush()
        extras.append(chunk)

    other = _seed_chunk(
        db_session,
        code=jur_code,
        text_value="Skilled Worker visa salary threshold Certificate of Sponsorship.",
        url_suffix="sw-b",
    )
    third = _seed_chunk(
        db_session,
        code=jur_code,
        text_value="Skilled Worker visa English language requirement guidance.",
        url_suffix="sw-c",
    )
    fake = FakeEmbedder()
    for chunk in [base, *extras, other, third]:
        _embed_chunk(db_session, chunk, fake)

    retriever = InternalKnowledgeRetriever(db_session, embedder=fake)
    # Same-doc chunks ranked first — diversification must surface other docs.
    retriever._semantic_search = (  # type: ignore[method-assign]
        lambda query, jurisdiction, limit: [
            (base.id, 0.99),
            (extras[0].id, 0.98),
            (extras[1].id, 0.97),
            (other.id, 0.90),
            (third.id, 0.85),
        ]
    )
    result = retriever.retrieve(
        InternalQuery(
            query="Skilled Worker visa eligibility licensed sponsor",
            jurisdiction="GB",
            mode="semantic",
            top_k=3,
            diversify_documents=True,
            max_chunks_per_document=1,
            document_aware_passages=False,
        )
    )
    assert result.status == RetrievalStatus.OK
    doc_ids = [p.document_id for p in result.passages]
    assert len(doc_ids) == 3
    assert len(set(doc_ids)) == 3
    assert doc_ids[0] == base.document_id


def test_document_aware_selects_stronger_within_doc_chunk(db_session: Session):
    """Within-doc reselection should prefer the stronger passage in a hit doc."""
    overview = _seed_chunk(
        db_session,
        code="CA",
        text_value=(
            "Federal Skilled Worker Program The Federal Skilled Worker Program "
            "is for skilled workers who have foreign work experience."
        ),
        url_suffix="fsw",
        heading="Federal Skilled Worker Program",
    )
    weaker = DocumentChunk(
        document_id=overview.document_id,
        document_version_id=overview.document_version_id,
        source_id=overview.source_id,
        jurisdiction_id=overview.jurisdiction_id,
        jurisdiction_code="CA",
        chunk_index=1,
        content_text="Selection factors points table unrelated boilerplate filler.",
        content_sha256=_sha("selection-factors"),
        is_current_version=True,
        heading_path="Federal Skilled Worker Program > Selection factors",
    )
    db_session.add(weaker)
    db_session.flush()
    fake = FakeEmbedder()
    _embed_chunk(db_session, overview, fake)
    _embed_chunk(db_session, weaker, fake)

    retriever = InternalKnowledgeRetriever(db_session, embedder=fake)
    # Global pool ranks the weaker chunk first; within-doc stub prefers overview.
    retriever._semantic_search = (  # type: ignore[method-assign]
        lambda query, jurisdiction, limit: [
            (weaker.id, 0.99),
            (overview.id, 0.80),
        ]
    )
    retriever._semantic_search_in_document = (  # type: ignore[method-assign]
        lambda jurisdiction, document_id, limit, query_vec_literal: [
            (overview.id, 0.95),
            (weaker.id, 0.80),
        ]
    )
    result = retriever.retrieve(
        InternalQuery(
            query="Federal Skilled Worker Program overview",
            jurisdiction="CA",
            mode="semantic",
            top_k=2,
            diversify_documents=True,
            document_aware_passages=True,
            max_chunks_per_document=2,
            expand_adjacent_chunks=False,
        )
    )
    assert result.status == RetrievalStatus.OK
    assert result.passages[0].chunk_id == overview.id
