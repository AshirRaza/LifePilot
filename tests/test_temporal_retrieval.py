"""Phase 10B.3 tests: date-aware retrieval, LegalRule lookup, and historical selection.

All fixtures and rules are strictly synthetic test records against ``lifepilot_test``.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lifepilot.agent.answer import (
    AnswerClaim,
    GroundedAnswer,
    build_user_prompt,
    pack_evidence_context,
)
from lifepilot.agent.config import AgentConfig, reset_agent_config_cache
from lifepilot.agent.contracts import AgentRequest, AgentStatus
from lifepilot.agent.evidence import (
    EvidenceSourceType,
    EvidenceVerificationState,
    evidence_from_external_item,
    evidence_from_internal_passage,
)
from lifepilot.agent.graph import reset_compiled_agent_graph
from lifepilot.agent.orchestration import (
    EvidenceOrchestrator,
    reset_evidence_orchestrator_override,
    set_evidence_orchestrator_override,
)
from lifepilot.agent.providers import (
    DisabledLLMProvider,
    LLMUsage,
    StructuredLLMResult,
    reset_llm_provider_override,
    set_llm_provider_override,
)
from lifepilot.agent.retrieval_policy import ExternalMode, decide_retrieval
from lifepilot.agent.runner import run_agent
from lifepilot.agent.temporal_selection import (
    TemporalOutcomeStatus,
    TemporalQueryMode,
    resolve_temporal_context,
    select_temporal_evidence,
)
from lifepilot.api.rate_limit import reset_rate_limit_state
from lifepilot.api.settings import reset_api_settings_cache
from lifepilot.crawl.storage import extraction_file_path, relative_storage_path
from lifepilot.db.models import (
    PLANNED_EMBEDDING_MODEL_ID,
    Authority,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    DocumentExtraction,
    DocumentVersion,
    Jurisdiction,
    LegalRule,
    Source,
)
from lifepilot.external.cache import ResponseCache
from lifepilot.external.http import ExternalHttpClient, HttpClientConfig
from lifepilot.external.models import ProviderId
from lifepilot.external.router import ExternalKnowledgeRouter
from lifepilot.rag.embed_pipeline import run_embedding
from lifepilot.rag.embedder import FakeEmbedder
from lifepilot.rag.legal_rules import query_reviewed_legal_rules, rule_applies_on_date
from lifepilot.rag.pipeline import run_chunking
from lifepilot.rag.retrieval import InternalKnowledgeRetriever, InternalQuery


def _sha(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


def _seed_doc_with_versions(
    session: Session,
    *,
    code: str = "GB",
    url_suffix: str = "route",
    versions: list[dict],
    embedder: FakeEmbedder | None = None,
) -> dict:
    jur = session.scalar(select(Jurisdiction).where(Jurisdiction.code == code))
    if jur is None:
        jur = Jurisdiction(
            code=code,
            name=f"Synthetic {code}",
            jurisdiction_type="supranational" if code == "EU" else "country",
            country_code=None if code == "EU" else code,
        )
        session.add(jur)
        session.flush()
    auth = Authority(jurisdiction_id=jur.id, name=f"Auth-{code}-{url_suffix}")
    session.add(auth)
    session.flush()
    source = Source(
        authority_id=auth.id,
        jurisdiction_id=jur.id,
        name=f"Src-{code}-{url_suffix}",
        seed_url=f"https://example.test/{code.lower()}/{url_suffix}",
        seed_url_normalized=f"https://example.test/{code.lower()}/{url_suffix}",
        review_status="approved",
    )
    session.add(source)
    session.flush()
    doc = Document(
        authority_id=auth.id,
        primary_source_id=source.id,
        canonical_url=f"https://example.test/{code.lower()}/{url_suffix}/guide",
        url_normalized=f"https://example.test/{code.lower()}/{url_suffix}/guide",
        content_kind="html_page",
        language="en",
        title=f"Synthetic {code} {url_suffix}",
        discovery_status="seed",
    )
    session.add(doc)
    session.flush()

    created_versions: list[DocumentVersion] = []
    created_chunks: list[DocumentChunk] = []
    for i, spec in enumerate(versions, start=1):
        ver = DocumentVersion(
            document_id=doc.id,
            fetched_via_source_id=source.id,
            version_number=spec.get("version_number", i),
            source_url=doc.canonical_url,
            content_type="text/html",
            language="en",
            retrieved_at=spec.get("retrieved_at") or datetime.now(timezone.utc),
            source_published_at=spec.get("source_published_at"),
            source_updated_at=spec.get("source_updated_at"),
            legal_status=spec.get("legal_status", "unknown"),
            http_status=200,
            content_sha256=_sha(f"{code}-{url_suffix}-v{i}-{spec.get('text','')}"),
            byte_size=len(spec.get("text") or "x"),
            storage_path=f"raw/documents/{doc.id}/v{i}.html",
            is_current=bool(spec.get("is_current", False)),
        )
        session.add(ver)
        session.flush()
        created_versions.append(ver)

        text_val = spec.get("text")
        if text_val:
            chunk = DocumentChunk(
                document_id=doc.id,
                document_version_id=ver.id,
                source_id=source.id,
                jurisdiction_id=jur.id,
                jurisdiction_code=code,
                chunk_index=0,
                heading_path=spec.get("heading_path") or "Requirements",
                content_text=text_val,
                content_sha256=_sha(text_val),
                is_current_version=bool(spec.get("is_current", False)),
            )
            session.add(chunk)
            session.flush()
            created_chunks.append(chunk)
            if embedder is not None:
                vec = embedder.embed_passages([text_val]).vectors[0]
                session.add(
                    DocumentChunkEmbedding(
                        chunk_id=chunk.id,
                        embedding_model_id=PLANNED_EMBEDDING_MODEL_ID,
                        embedding=list(map(float, vec.tolist())),
                    )
                )
                session.flush()

    return {
        "jurisdiction": jur,
        "authority": auth,
        "source": source,
        "document": doc,
        "versions": created_versions,
        "chunks": created_chunks,
    }


def _make_verified_rule(g: dict, **overrides) -> LegalRule:
    """Build a LegalRule. Persist as unverified; call ``_authorize_rules`` after flush."""
    payload = {
        "rule_key": "xx_route.synthetic_threshold",
        "rule_revision": 1,
        "jurisdiction_id": g["jurisdiction"].id,
        "jurisdiction_code": g["jurisdiction"].code,
        "pathway_id": "xx_route",
        "requirement_category": "financial_or_sponsorship",
        "requirement_type": "salary_threshold",
        "numeric_value": Decimal("30000.00"),
        "numeric_comparator": "gte",
        "currency_code": "GBP",
        "amount_unit": "yearly",
        "effective_from": date(2024, 1, 1),
        "effective_to": None,
        "effective_from_inclusive": True,
        "effective_to_inclusive": True,
        "effective_date_status": "known_open_ended",
        "cohort_key": "general",
        "cohort_category": "general",
        "is_transitional": False,
        "cohort_conditions": {},
        "rule_summary": "Synthetic threshold rule.",
        "document_id": g["document"].id,
        "document_version_id": g["versions"][0].id,
        "source_id": g["source"].id,
        "source_url": g["document"].canonical_url,
        # Insert-safe defaults; promote via ``_authorize_rules`` (Phase 10D.4).
        "review_status": "unreviewed",
        "verification_status": "unverified",
        "reviewed_by": None,
        "reviewed_at": None,
    }
    payload.update(overrides)
    # Strip legacy direct human_verified inserts (trigger + invariant require events).
    if payload.get("verification_status") == "human_verified":
        payload["verification_status"] = "unverified"
        payload["review_status"] = "unreviewed"
        payload["reviewed_by"] = None
        payload["reviewed_at"] = None
    return LegalRule(**payload)


def _authorize_rules(session: Session, rules: list[LegalRule]) -> list[LegalRule]:
    from lifepilot.governance.synthetic_authority import ensure_synthetic_human_authority

    out: list[LegalRule] = []
    for rule in rules:
        if rule.verification_status == "unverified" and rule.review_status == "unreviewed":
            out.append(ensure_synthetic_human_authority(session, rule))
        else:
            out.append(rule)
    return out


@pytest.fixture()
def _clean_runtime(reset_agent_runtime):
    yield


# ---------------------------------------------------------------------------
# 1–2. Temporal context resolution & mode distinction
# ---------------------------------------------------------------------------


def test_temporal_context_distinguishes_omitted_vs_explicit_same_date():
    today = date(2026, 11, 18)
    ref1, mode1 = resolve_temporal_context(None, today_utc=today)
    ref2, mode2 = resolve_temporal_context(today, today_utc=today)
    ref3, mode3 = resolve_temporal_context(date(2020, 6, 1), today_utc=today)
    ref4, mode4 = resolve_temporal_context(date(2027, 4, 1), today_utc=today)

    assert ref1 == today and mode1 == TemporalQueryMode.CURRENT
    assert ref2 == today and mode2 == TemporalQueryMode.EXPLICIT_CURRENT
    assert ref3 == date(2020, 6, 1) and mode3 == TemporalQueryMode.HISTORICAL
    assert ref4 == date(2027, 4, 1) and mode4 == TemporalQueryMode.FUTURE


def test_run_agent_propagates_as_of_date_and_temporal_metadata(
    db_session: Session, _clean_runtime
):
    fake_emb = FakeEmbedder()
    g = _seed_doc_with_versions(
        db_session,
        code="GB",
        url_suffix="sw-prop",
        versions=[
            {
                "version_number": 1,
                "is_current": True,
                "text": "Synthetic current route requirement: licensed employer needed.",
            }
        ],
        embedder=fake_emb,
    )

    @contextmanager
    def _bound():
        yield db_session

    orch = EvidenceOrchestrator(session_factory=_bound, embedder=fake_emb)
    set_evidence_orchestrator_override(orch)
    set_llm_provider_override(DisabledLLMProvider())

    resp_cur = run_agent(
        AgentRequest(
            question="Licensed employer requirement?",
            jurisdiction="GB",
            retrieval={"external_mode": "off"},
        )
    )
    assert resp_cur.as_of_date is None
    assert resp_cur.resolved_reference_date == datetime.now(timezone.utc).date()
    assert resp_cur.temporal_mode == "current"
    assert resp_cur.temporal_outcome == TemporalOutcomeStatus.CURRENT_CANDIDATE_UNVERIFIED.value

    today = datetime.now(timezone.utc).date()
    resp_exp = run_agent(
        AgentRequest(
            question="Licensed employer requirement?",
            jurisdiction="GB",
            as_of_date=today,
            retrieval={"external_mode": "off"},
        )
    )
    assert resp_exp.as_of_date == today
    assert resp_exp.resolved_reference_date == today
    assert resp_exp.temporal_mode == "explicit_current"


# ---------------------------------------------------------------------------
# 3–4. Historical v1 vs v2/v3 versions & missing historical evidence abstention
# ---------------------------------------------------------------------------


def test_historical_as_of_date_retrieves_v1_and_excludes_generic_v2(
    db_session: Session, _clean_runtime
):
    fake_emb = FakeEmbedder()
    _seed_doc_with_versions(
        db_session,
        code="GB",
        url_suffix="hist-versions",
        versions=[
            {
                "version_number": 1,
                "is_current": False,
                "legal_status": "historical",
                "source_published_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
                "text": (
                    "Historical v1 synthetic threshold for route Alpha was 20,000 units "
                    "with approved employer sponsorship."
                ),
            },
            {
                "version_number": 2,
                "is_current": True,
                "legal_status": "unknown",
                "source_published_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                "text": (
                    "Current v2 synthetic threshold for route Alpha is 99,000 units "
                    "with approved employer sponsorship."
                ),
            },
        ],
        embedder=fake_emb,
    )

    @contextmanager
    def _bound():
        yield db_session

    orch = EvidenceOrchestrator(session_factory=_bound, embedder=fake_emb)
    res_hist = orch.gather(
        question="What was the synthetic threshold for route Alpha?",
        jurisdiction="GB",
        as_of_date=date(2020, 6, 1),
        external_mode="auto",
    )
    assert res_hist.status == AgentStatus.SUCCESS
    assert res_hist.temporal_mode == "historical"
    assert res_hist.temporal_outcome == TemporalOutcomeStatus.HISTORICAL_CANDIDATE_UNVERIFIED.value
    assert len(res_hist.evidence) == 1
    assert "20,000" in res_hist.evidence[0].passage_text
    assert "99,000" not in res_hist.evidence[0].passage_text

    # Current-law query gets v2 ONLY and never leaks v1.
    res_cur = orch.gather(
        question="What is the synthetic threshold for route Alpha?",
        jurisdiction="GB",
        external_mode="off",
    )
    assert res_cur.status == AgentStatus.SUCCESS
    assert len(res_cur.evidence) == 1
    assert "99,000" in res_cur.evidence[0].passage_text
    assert "20,000" not in res_cur.evidence[0].passage_text


def test_missing_historical_evidence_abstains_without_current_substitution(
    db_session: Session, _clean_runtime
):
    fake_emb = FakeEmbedder()
    _seed_doc_with_versions(
        db_session,
        code="GB",
        url_suffix="only-current",
        versions=[
            {
                "version_number": 1,
                "is_current": True,
                "legal_status": "unknown",
                "source_published_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "text": "Current synthetic fee for route Beta is 88,888 units.",
            }
        ],
        embedder=fake_emb,
    )

    @contextmanager
    def _bound():
        yield db_session

    orch = EvidenceOrchestrator(session_factory=_bound, embedder=fake_emb)
    set_evidence_orchestrator_override(orch)

    class _BoomLLM:
        def complete_structured(self, *a, **k):
            raise AssertionError("LLM must not be called when historical evidence missing")

    set_llm_provider_override(_BoomLLM())

    resp = run_agent(
        AgentRequest(
            question="What was the synthetic fee for route Beta in 2020?",
            jurisdiction="GB",
            as_of_date=date(2020, 6, 1),
        )
    )
    assert resp.status == AgentStatus.INSUFFICIENT_EVIDENCE
    assert resp.answer is None
    assert resp.evidence == []
    assert resp.temporal_outcome == TemporalOutcomeStatus.CURRENT_AVAILABLE_HISTORICAL_UNAVAILABLE.value
    assert any("current_rules_not_substituted" in w for w in resp.warnings)


# ---------------------------------------------------------------------------
# 5–7. Archived, withdrawn, and proposed source handling
# ---------------------------------------------------------------------------


def test_archived_excluded_from_current_but_retained_for_historical():
    archived_item = evidence_from_internal_passage(
        jurisdiction="IE",
        official_url="https://enterprise.gov.ie/archived-notices.html",
        document_id=50,
        document_version_id=51,
        chunk_id=501,
        content_text="Historical archived notice from 2022 stating 22,000 synthetic units.",
        legal_status="archived",
        source_published_at="2022-05-01T00:00:00+00:00",
        is_current_version=True,
    )

    cur_sel = select_temporal_evidence(
        [archived_item],
        resolved_reference_date=date(2026, 11, 18),
        temporal_mode=TemporalQueryMode.CURRENT,
    )
    assert cur_sel.kept_evidence == []
    assert cur_sel.temporal_outcome == TemporalOutcomeStatus.EXCLUDED_NON_APPLICABLE_STATUS
    assert cur_sel.excluded_items[0]["reason"] == "excluded_status_for_current:archived"

    hist_sel = select_temporal_evidence(
        [archived_item],
        resolved_reference_date=date(2022, 10, 1),
        temporal_mode=TemporalQueryMode.HISTORICAL,
    )
    assert len(hist_sel.kept_evidence) == 1
    assert hist_sel.temporal_outcome == TemporalOutcomeStatus.HISTORICAL_CANDIDATE_UNVERIFIED


def test_proposed_and_withdrawn_evidence_selection():
    withdrawn = evidence_from_external_item(
        jurisdiction="GB",
        official_url="https://www.gov.uk/withdrawn",
        provider="govuk_content",
        passage_text="Withdrawn guidance text that is no longer valid.",
        legal_status="withdrawn",
    )
    proposed = evidence_from_external_item(
        jurisdiction="US",
        official_url="https://www.federalregister.gov/d/2026-1",
        provider="us_federal_register",
        passage_text="Proposed rule not yet enacted.",
        published_at="2026-09-01",
        effective_at="2027-01-01",
        legal_status="proposed_not_effective",
    )

    cur = select_temporal_evidence(
        [withdrawn, proposed],
        resolved_reference_date=date(2026, 11, 18),
        temporal_mode=TemporalQueryMode.CURRENT,
    )
    assert cur.kept_evidence == []
    assert cur.temporal_outcome == TemporalOutcomeStatus.EXCLUDED_NON_APPLICABLE_STATUS

    hist = select_temporal_evidence(
        [proposed],
        resolved_reference_date=date(2026, 10, 15),
        temporal_mode=TemporalQueryMode.HISTORICAL,
    )
    assert hist.kept_evidence == []


# ---------------------------------------------------------------------------
# 8–13. Curated LegalRule lookup, boundaries, cohorts, verification, isolation
# ---------------------------------------------------------------------------


def test_legal_rule_boundaries_inclusive_exclusive_and_unknown_dates(db_session: Session):
    g = _seed_doc_with_versions(
        db_session,
        code="GB",
        url_suffix="rules-bounds",
        versions=[{"version_number": 1, "is_current": True, "text": "Base rule doc."}],
    )

    unknown_rule = _make_verified_rule(
        g,
        rule_key="xx_route.unknown",
        effective_from=None,
        effective_to=None,
        effective_date_status="unknown",
    )
    partial_rule = _make_verified_rule(
        g,
        rule_key="xx_route.partial",
        effective_from=date(2024, 1, 1),
        effective_to=None,
        effective_date_status="partially_known",
    )
    bounded_half_open = _make_verified_rule(
        g,
        rule_key="xx_route.half_open",
        numeric_value=Decimal("25000.00"),
        effective_from=date(2024, 4, 4),
        effective_to=date(2030, 4, 4),
        effective_from_inclusive=True,
        effective_to_inclusive=False,
        effective_date_status="known_bounded",
    )
    exclusive_start = _make_verified_rule(
        g,
        rule_key="xx_route.excl_start",
        numeric_value=Decimal("28000.00"),
        effective_from=date(2024, 4, 4),
        effective_to=date(2025, 4, 4),
        effective_from_inclusive=False,
        effective_to_inclusive=True,
        effective_date_status="known_bounded",
    )
    db_session.add_all([unknown_rule, partial_rule, bounded_half_open, exclusive_start])
    db_session.flush()
    _authorize_rules(
        db_session, [unknown_rule, partial_rule, bounded_half_open, exclusive_start]
    )

    assert rule_applies_on_date(unknown_rule, date(2026, 1, 1))[0] is False
    assert rule_applies_on_date(partial_rule, date(2026, 1, 1))[0] is False

    # Boundary checks on [2024-04-04, 2030-04-04)
    assert rule_applies_on_date(bounded_half_open, date(2024, 4, 3))[0] is False
    assert rule_applies_on_date(bounded_half_open, date(2024, 4, 4))[0] is True
    assert rule_applies_on_date(bounded_half_open, date(2030, 4, 3))[0] is True
    assert rule_applies_on_date(bounded_half_open, date(2030, 4, 4))[0] is False

    # Boundary checks on (2024-04-04, 2025-04-04]
    assert rule_applies_on_date(exclusive_start, date(2024, 4, 4))[0] is False
    assert rule_applies_on_date(exclusive_start, date(2024, 4, 5))[0] is True
    assert rule_applies_on_date(exclusive_start, date(2025, 4, 4))[0] is True

    res = query_reviewed_legal_rules(
        db_session,
        jurisdiction="GB",
        pathway_id="xx_route",
        as_of_date=date(2024, 4, 4),
    )
    assert [r.rule_key for r in res.matched_rules] == ["xx_route.half_open"]
    assert res.unknown_date_excluded_count == 2


def test_legal_rule_cohorts_unreviewed_exclusion_and_isolation(db_session: Session):
    g_gb = _seed_doc_with_versions(
        db_session,
        code="GB",
        url_suffix="gb-cohort",
        versions=[{"version_number": 1, "is_current": True, "text": "GB rule doc."}],
    )
    g_ie = _seed_doc_with_versions(
        db_session,
        code="IE",
        url_suffix="ie-cohort",
        versions=[{"version_number": 1, "is_current": True, "text": "IE rule doc."}],
    )

    general_rule = _make_verified_rule(
        g_gb,
        rule_key="xx_gb.general",
        pathway_id="xx_gb_route",
        numeric_value=Decimal("40000.00"),
        cohort_key="general",
        cohort_category="general",
        is_transitional=False,
    )
    transitional_rule = _make_verified_rule(
        g_gb,
        rule_key="xx_gb.transitional",
        pathway_id="xx_gb_route",
        numeric_value=Decimal("31000.00"),
        cohort_key="transitional_legacy",
        cohort_category="transitional",
        is_transitional=True,
        cohort_conditions={"prior_permission_before": "2024-04-04", "continuous_status": True},
        effective_from=date(2024, 4, 4),
        effective_to=date(2030, 4, 4),
        effective_to_inclusive=False,
        effective_date_status="known_bounded",
    )
    unreviewed_rule = _make_verified_rule(
        g_gb,
        rule_key="xx_gb.unreviewed",
        pathway_id="xx_gb_route",
        numeric_value=Decimal("10000.00"),
        review_status="unreviewed",
        verification_status="unverified",
        reviewed_by=None,
        reviewed_at=None,
    )
    other_pathway_rule = _make_verified_rule(
        g_gb,
        rule_key="xx_gb.other_pathway",
        pathway_id="xx_gb_other",
        numeric_value=Decimal("55000.00"),
    )
    ie_rule = _make_verified_rule(
        g_ie,
        rule_key="xx_ie.general",
        pathway_id="xx_ie_route",
        numeric_value=Decimal("38000.00"),
        currency_code="EUR",
    )
    db_session.add_all(
        [general_rule, transitional_rule, unreviewed_rule, other_pathway_rule, ie_rule]
    )
    db_session.flush()
    _authorize_rules(
        db_session, [general_rule, transitional_rule, other_pathway_rule, ie_rule]
    )

    # Unknown applicant cohort: returns general ONLY, keeps transitional as caveat, excludes unreviewed & other pathways/jurisdictions.
    unknown_cohort = query_reviewed_legal_rules(
        db_session,
        jurisdiction="GB",
        pathway_id="xx_gb_route",
        as_of_date=date(2026, 6, 1),
        user_circumstances=None,
    )
    assert [r.rule_key for r in unknown_cohort.matched_rules] == ["xx_gb.general"]
    assert [r.rule_key for r in unknown_cohort.cohort_alternatives] == ["xx_gb.transitional"]
    assert unknown_cohort.unreviewed_excluded_count == 1
    assert unknown_cohort.cohort_clarification_required is False

    # Confirmed transitional cohort via structured conditions: returns transitional rule.
    matched_trans = query_reviewed_legal_rules(
        db_session,
        jurisdiction="GB",
        pathway_id="xx_gb_route",
        as_of_date=date(2026, 6, 1),
        user_circumstances={
            "prior_permission_date": "2023-06-01",
            "continuous_status": True,
        },
    )
    assert {r.rule_key for r in matched_trans.matched_rules} == {
        "xx_gb.general",
        "xx_gb.transitional",
    }

    # Cohort-only pathway with unknown applicant cohort triggers cohort_clarification_required.
    only_trans = _make_verified_rule(
        g_gb,
        rule_key="xx_gb_cohort_only.trans",
        pathway_id="xx_gb_cohort_only",
        numeric_value=Decimal("22000.00"),
        cohort_key="age_under_30",
        cohort_category="age_based",
        cohort_conditions={"age_under": 30},
    )
    db_session.add(only_trans)
    db_session.flush()
    _authorize_rules(db_session, [only_trans])
    cohort_missing = query_reviewed_legal_rules(
        db_session,
        jurisdiction="GB",
        pathway_id="xx_gb_cohort_only",
        as_of_date=date(2026, 6, 1),
    )
    assert cohort_missing.matched_rules == []
    assert cohort_missing.cohort_clarification_required is True

    # Jurisdiction isolation: IE query never returns GB rules.
    ie_only = query_reviewed_legal_rules(
        db_session,
        jurisdiction="IE",
        pathway_id="xx_ie_route",
        as_of_date=date(2026, 6, 1),
    )
    assert [r.rule_key for r in ie_only.matched_rules] == ["xx_ie.general"]


# ---------------------------------------------------------------------------
# 14. Historical reindexing idempotency and deduplication
# ---------------------------------------------------------------------------


def test_historical_chunking_and_embedding_backfill_idempotent(
    db_session: Session, tmp_path: Path
):
    data = tmp_path / "data"
    g = _seed_doc_with_versions(
        db_session,
        code="GB",
        url_suffix="backfill",
        versions=[
            {
                "version_number": 1,
                "is_current": False,
                "legal_status": "historical",
            },
            {
                "version_number": 2,
                "is_current": False,
                "legal_status": "historical",
            },
            {
                "version_number": 3,
                "is_current": True,
                "legal_status": "unknown",
            },
        ],
    )
    doc = g["document"]
    v1, v2, v3 = g["versions"]

    def _add_ext(ver: DocumentVersion, body_text: str, ext_sha: str) -> None:
        path = extraction_file_path(
            data, doc.id, ver.version_number, extractor_name="html_structured"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 2,
            "extractor": "html_structured",
            "extractor_version": "1.3.2",
            "document_id": doc.id,
            "title": "Synthetic Historical Guide",
            "blocks": [
                {"type": "heading", "level": 1, "text": "Rules"},
                {"type": "paragraph", "text": body_text},
            ],
            "content_sha256": ext_sha,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        db_session.add(
            DocumentExtraction(
                document_version_id=ver.id,
                extractor_name="html_structured",
                extractor_version="1.3.2",
                status="success",
                content_sha256=ext_sha,
                storage_path=relative_storage_path(data, path),
                title="Synthetic Historical Guide",
                extracted_at=datetime.now(timezone.utc),
            )
        )

    # v1 and v2 share identical extracted content (v2 should be deduped!).
    _add_ext(
        v1,
        "Historical 2020 synthetic requirement: employers must hold valid sponsorship.",
        _sha("hist-body"),
    )
    _add_ext(
        v2,
        "Historical 2020 synthetic requirement: employers must hold valid sponsorship.",
        _sha("hist-body"),
    )
    _add_ext(
        v3,
        "Current 2026 synthetic requirement: updated eligibility applies today.",
        _sha("curr-body"),
    )
    db_session.flush()

    # Default run_chunking indexes current version only (v3).
    default_stats = run_chunking(db_session, data_dir=data, force=False)
    assert default_stats.processed == 1
    assert (
        db_session.scalar(
            select(func.count()).select_from(DocumentChunk).where(
                DocumentChunk.document_version_id == v1.id
            )
        )
        == 0
    )

    # Historical run_chunking indexes v1 (distinct) and dedupes v2.
    hist_stats = run_chunking(
        db_session, data_dir=data, include_historical=True, dedupe_historical=True
    )
    assert hist_stats.processed == 1
    assert hist_stats.skipped_duplicate_historical == 1
    assert hist_stats.skipped_unchanged == 1

    # Second historical run is idempotent.
    hist_stats2 = run_chunking(
        db_session, data_dir=data, include_historical=True, dedupe_historical=True
    )
    assert hist_stats2.processed == 0

    v1_chunk = db_session.scalar(
        select(DocumentChunk).where(DocumentChunk.document_version_id == v1.id)
    )
    v3_chunk = db_session.scalar(
        select(DocumentChunk).where(DocumentChunk.document_version_id == v3.id)
    )
    assert v1_chunk is not None and v1_chunk.is_current_version is False
    assert v3_chunk is not None and v3_chunk.is_current_version is True

    # Default embedding skips v1; historical embedding embeds v1 idempotently.
    fake_emb = FakeEmbedder()
    e_cur = run_embedding(db_session, embedder=fake_emb, include_historical=False)
    assert e_cur.embedded == 1
    e_hist = run_embedding(db_session, embedder=fake_emb, include_historical=True)
    assert e_hist.embedded == 1
    e_hist2 = run_embedding(db_session, embedder=fake_emb, include_historical=True)
    assert e_hist2.eligible_chunks == 0


# ---------------------------------------------------------------------------
# 15–18. API compatibility, external safeguard, conflicts, prompt budget
# ---------------------------------------------------------------------------


def test_api_accepts_as_of_date_and_preserves_compatibility(monkeypatch):
    monkeypatch.setenv("LIFEPILOT_ENV", "development")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_AUTH", "1")
    monkeypatch.setenv("LIFEPILOT_API_KEYS", "api-test-key")
    monkeypatch.setenv("LIFEPILOT_VERIFICATION_ENABLED", "1")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_LLM", "0")
    reset_api_settings_cache()
    reset_agent_config_cache()
    reset_rate_limit_state()

    import lifepilot.api.app as api_app
    from lifepilot.agent.contracts import AgentResponse, TimingMetadata

    captured: dict = {}

    def _fake_run(req: AgentRequest, config=None) -> AgentResponse:
        captured["req"] = req
        ref, tmode = resolve_temporal_context(req.as_of_date)
        return AgentResponse(
            request_id="req_api_1",
            status=AgentStatus.INSUFFICIENT_EVIDENCE,
            answer=None,
            claims=[],
            verification={"skipped": True},
            timing=TimingMetadata(total_ms=1.0),
            jurisdiction=req.jurisdiction,
            pathway_id=req.pathway_id,
            as_of_date=req.as_of_date,
            resolved_reference_date=ref,
            temporal_mode=tmode.value,
            temporal_outcome=TemporalOutcomeStatus.INSUFFICIENT_FOR_DATE.value,
            retrieval_decision="internal_only",
        )

    monkeypatch.setattr(api_app, "run_agent", _fake_run)
    client = TestClient(api_app.create_app())
    headers = {"X-API-Key": "api-test-key"}

    # Backward-compatible payload without as_of_date.
    r1 = client.post(
        "/v1/answer",
        headers=headers,
        json={"question": "Synthetic question", "jurisdiction": "GB"},
    )
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["as_of_date"] is None
    assert b1["temporal_mode"] == "current"
    assert b1["resolved_reference_date"] == datetime.now(timezone.utc).date().isoformat()

    # Historical as_of_date accepted and echoed.
    r2 = client.post(
        "/v1/answer",
        headers=headers,
        json={
            "question": "Synthetic question",
            "jurisdiction": "GB",
            "as_of_date": "2020-06-01",
        },
    )
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["as_of_date"] == "2020-06-01"
    assert b2["resolved_reference_date"] == "2020-06-01"
    assert b2["temporal_mode"] == "historical"
    assert captured["req"].as_of_date == date(2020, 6, 1)

    # Malformed date rejected with 422.
    r3 = client.post(
        "/v1/answer",
        headers=headers,
        json={
            "question": "Synthetic question",
            "jurisdiction": "GB",
            "as_of_date": "not-a-date",
        },
    )
    assert r3.status_code == 422

    # Auth still enforced.
    assert (
        client.post(
            "/v1/answer",
            json={"question": "Synthetic question", "jurisdiction": "GB"},
        ).status_code
        == 401
    )
    reset_api_settings_cache()
    reset_agent_config_cache()
    reset_rate_limit_state()


def test_historical_external_safeguard_blocks_live_current_connectors():
    # Policy denies live current connectors when explicit historical mode is set.
    decision_gb = decide_retrieval(
        question="What was the UK Skilled Worker salary threshold?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        external_mode=ExternalMode.AUTO,
        substantive_internal_count=0,
        internal_status="no_results",
        connectors_available=True,
        temporal_mode="historical",
        historical_connectors_available=False,
    )
    assert decision_gb.run_external is False

    # Even with external_mode="on", GB has no historical-date connector, so no HTTP call occurs.
    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "document_number": "2020-001",
                        "title": "Historical Notice",
                        "type": "Rule",
                        "abstract": "Published before 2020-06-01 regarding H-1B procedures.",
                        "html_url": "https://www.federalregister.gov/d/2020-001",
                        "publication_date": "2020-03-01",
                        "effective_on": "2020-04-01",
                    }
                ]
            },
            request=request,
        )

    transport = httpx.MockTransport(_handler)
    raw = httpx.Client(transport=transport)
    http = ExternalHttpClient(
        config=HttpClientConfig(timeout_seconds=5.0, max_retries=1),
        cache=ResponseCache(default_ttl_seconds=60.0, disk_dir=None),
        client=raw,
        allowed_hosts=None,
    )
    router = ExternalKnowledgeRouter(http=http, enable_disk_cache=False)

    @contextmanager
    def _dummy_sf():
        yield MagicMock()

    orch = EvidenceOrchestrator(session_factory=_dummy_sf, external_router=router)
    orch._retrieve_internal = lambda **_kw: ([], "no_results", 0.5)  # type: ignore[method-assign]

    gb_hist = orch.gather(
        question="Salary threshold?",
        jurisdiction="GB",
        as_of_date=date(2020, 6, 1),
        external_mode="on",
    )
    assert calls == []
    assert gb_hist.status == AgentStatus.INSUFFICIENT_EVIDENCE
    assert gb_hist.temporal_outcome == TemporalOutcomeStatus.INSUFFICIENT_FOR_DATE.value

    # US Federal Register connector supports published_before date filtering.
    us_hist = orch.gather(
        question="H-1B rule",
        jurisdiction="US",
        as_of_date=date(2020, 6, 1),
        external_mode="on",
    )
    assert len(calls) == 1
    assert "2020-06-01" in calls[0]
    assert us_hist.evidence
    assert us_hist.temporal_outcome == TemporalOutcomeStatus.HISTORICAL_CANDIDATE_UNVERIFIED.value


def test_reviewed_legal_rule_orchestration_conflict_and_prompt_budget(
    db_session: Session, _clean_runtime
):
    fake_emb = FakeEmbedder()
    g = _seed_doc_with_versions(
        db_session,
        code="GB",
        url_suffix="curated-orch",
        versions=[
            {
                "version_number": 1,
                "is_current": False,
                "legal_status": "historical",
                "text": "Historical snapshot text for 2020 threshold of 25,600.",
            },
            {
                "version_number": 2,
                "is_current": True,
                "legal_status": "unknown",
                "text": "Current guidance text for general threshold.",
            },
        ],
        embedder=fake_emb,
    )
    rule_ok = _make_verified_rule(
        g,
        rule_key="xx_gb.sw_salary_2020",
        pathway_id="gb_skilled_worker",
        numeric_value=Decimal("25600.00"),
        currency_code="GBP",
        amount_unit="yearly",
        effective_from=date(2020, 1, 1),
        effective_to=date(2024, 4, 3),
        effective_date_status="known_bounded",
        rule_summary="Synthetic 2020 threshold was GBP 25,600 per year.",
    )
    db_session.add(rule_ok)
    db_session.flush()
    _authorize_rules(db_session, [rule_ok])

    @contextmanager
    def _bound():
        yield db_session

    orch = EvidenceOrchestrator(session_factory=_bound, embedder=fake_emb)
    res = orch.gather(
        question="What was the salary threshold?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        as_of_date=date(2022, 6, 1),
        external_mode="off",
    )
    assert res.status == AgentStatus.SUCCESS
    assert res.temporal_outcome == TemporalOutcomeStatus.REVIEWED_RULE_FOUND.value
    assert res.evidence[0].source_type == EvidenceSourceType.CURATED_RULE
    assert res.evidence[0].verification_state == EvidenceVerificationState.HUMAN_VERIFIED_RULE

    # Prompt packing keeps curated rule provenance without violating passage budgets.
    packed = pack_evidence_context(
        res.evidence,
        max_items=5,
        max_chars_per_passage=120,
        max_total_chars=240,
    )
    assert packed
    assert packed[0]["rule_key"] == "xx_gb.sw_salary_2020"
    assert packed[0]["cohort_key"] == "general"
    assert sum(len(b["passage_text"]) for b in packed) <= 280

    prompt = build_user_prompt(
        question="What was the salary threshold?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        user_circumstances=None,
        evidence_payload=packed,
        resolved_reference_date="2022-06-01",
        temporal_mode="historical",
    )
    assert "Reference legal date (as_of): 2022-06-01 (mode=historical)" in prompt
    assert "rule_key: xx_gb.sw_salary_2020" in prompt

    # Conflicting verified rules surface conflicting_temporal_evidence outcome.
    rule_conflict = _make_verified_rule(
        g,
        rule_key="xx_gb.sw_salary_2020_dup",
        pathway_id="gb_skilled_worker",
        numeric_value=Decimal("29999.00"),
        currency_code="GBP",
        amount_unit="yearly",
        effective_from=date(2020, 1, 1),
        effective_to=date(2024, 4, 3),
        effective_date_status="known_bounded",
        rule_summary="Conflicting synthetic threshold GBP 29,999.",
    )
    db_session.add(rule_conflict)
    db_session.flush()
    _authorize_rules(db_session, [rule_conflict])
    res_conf = orch.gather(
        question="What was the salary threshold?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        as_of_date=date(2022, 6, 1),
        external_mode="off",
    )
    assert res_conf.temporal_outcome == TemporalOutcomeStatus.CONFLICTING_TEMPORAL_EVIDENCE.value
    assert any("conflicting_verified_rules" in w for w in res_conf.warnings)


def test_unmigrated_schema_fallback_does_not_crash_legal_rule_lookup(
    db_session: Session, monkeypatch
):
    monkeypatch.setattr(
        "lifepilot.rag.legal_rules.legal_rules_table_available",
        lambda session: False,
    )
    res = query_reviewed_legal_rules(
        db_session,
        jurisdiction="GB",
        as_of_date=date(2026, 11, 18),
    )
    assert res.status == "schema_unavailable"
    assert res.matched_rules == []
