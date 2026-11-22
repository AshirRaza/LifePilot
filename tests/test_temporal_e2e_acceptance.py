"""Phase 10B.5 Integrated Temporal Accuracy Acceptance E2E tests.

Exercises the complete execution path:
API (/v1/answer) -> AgentRequest -> temporal intent -> EvidenceOrchestrator ->
LegalRule / InternalKnowledgeRetriever (PostgreSQL lifepilot_test) ->
temporal selection -> generation (mocked LLM) -> verify_material_claims ->
compose_grounded_answer_text -> AnswerResponse.

All rules/thresholds are strictly synthetic. Never modifies live lifepilot DB.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.agent.answer import AnswerClaim, GroundedAnswer
from lifepilot.agent.config import AgentConfig, reset_agent_config_cache
from lifepilot.agent.contracts import AgentRequest, AgentStatus
from lifepilot.agent.evidence import (
    EvidenceItem,
    EvidenceSourceType,
    evidence_from_external_item,
    evidence_from_internal_passage,
)
from lifepilot.agent.orchestration import (
    EvidenceOrchestrator,
    set_evidence_orchestrator_override,
)
from lifepilot.agent.providers import (
    LLMUsage,
    StructuredLLMResult,
    set_llm_provider_override,
)
from lifepilot.agent.runner import run_agent
from lifepilot.agent.temporal_selection import TemporalOutcomeStatus
from lifepilot.agent.verification import (
    ClaimVerificationStatus,
    verify_material_claims,
)
from lifepilot.api.rate_limit import reset_rate_limit_state
from lifepilot.api.settings import reset_api_settings_cache
from lifepilot.db.models import (
    PLANNED_EMBEDDING_MODEL_ID,
    Authority,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    DocumentVersion,
    Jurisdiction,
    LegalRule,
    Source,
)
from lifepilot.external.cache import ResponseCache
from lifepilot.external.http import ExternalHttpClient, HttpClientConfig
from lifepilot.external.router import ExternalKnowledgeRouter
from lifepilot.rag.embedder import FakeEmbedder


def _sha(text_val: str) -> str:
    return hashlib.sha256(text_val.encode("utf-8")).hexdigest()


class _ScenarioFakeLLM:
    """Deterministic fake LLM driven by a scenario claim builder."""

    provider_id = "fake"
    model_id = "fake-sonnet"

    def __init__(self, builder: Callable[[str], GroundedAnswer]) -> None:
        self._builder = builder
        self.calls = 0
        self.last_prompt: str = ""

    def complete(self, *a, **k):
        raise AssertionError("complete() must not be called")

    def complete_structured(self, messages, *, schema, max_tokens, temperature=0.0):
        self.calls += 1
        self.last_prompt = messages[-1].content
        value = self._builder(self.last_prompt)
        return StructuredLLMResult(
            value=value,
            usage=LLMUsage(input_tokens=50, output_tokens=40, total_tokens=90),
            latency_ms=3.0,
            model=self.model_id,
            provider=self.provider_id,
        )


def _extract_eids_from_prompt(prompt: str) -> list[tuple[str, str]]:
    """Return [(evidence_id, passage_block)] from packed prompt."""
    out: list[tuple[str, str]] = []
    for block in prompt.split("\n---\n"):
        if "evidence_id:" not in block:
            continue
        eid = ""
        for line in block.splitlines():
            if line.startswith("evidence_id: "):
                eid = line.split("evidence_id: ", 1)[1].strip()
                break
        if eid:
            out.append((eid, block))
    return out


def _seed_controlled_dataset(session: Session) -> dict:
    """Seed synthetic multi-version docs, archived/proposed sources, and LegalRules."""
    fake_emb = FakeEmbedder()

    jur_gb = Jurisdiction(
        code="GB", name="United Kingdom", jurisdiction_type="country", country_code="GB"
    )
    jur_ie = Jurisdiction(
        code="IE", name="Ireland", jurisdiction_type="country", country_code="IE"
    )
    jur_ca = Jurisdiction(
        code="CA", name="Canada", jurisdiction_type="country", country_code="CA"
    )
    session.add_all([jur_gb, jur_ie, jur_ca])
    session.flush()

    auth_gb = Authority(jurisdiction_id=jur_gb.id, name="Synthetic GB Authority")
    auth_ie = Authority(jurisdiction_id=jur_ie.id, name="Synthetic IE Authority")
    auth_ca = Authority(jurisdiction_id=jur_ca.id, name="Synthetic CA Authority")
    session.add_all([auth_gb, auth_ie, auth_ca])
    session.flush()

    src_gb = Source(
        authority_id=auth_gb.id,
        jurisdiction_id=jur_gb.id,
        name="Synthetic GB GOV",
        seed_url="https://www.gov.uk/skilled-worker-visa",
        seed_url_normalized="https://www.gov.uk/skilled-worker-visa",
        review_status="approved",
    )
    src_ie = Source(
        authority_id=auth_ie.id,
        jurisdiction_id=jur_ie.id,
        name="Synthetic IE DETE",
        seed_url="https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/",
        seed_url_normalized="https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/",
        review_status="approved",
    )
    src_ca = Source(
        authority_id=auth_ca.id,
        jurisdiction_id=jur_ca.id,
        name="Synthetic CA IRCC",
        seed_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry.html",
        seed_url_normalized="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry.html",
        review_status="approved",
    )
    session.add_all([src_gb, src_ie, src_ca])
    session.flush()

    def _add_doc_versions(src, jur, auth, url, title, versions_spec):
        doc = Document(
            authority_id=auth.id,
            primary_source_id=src.id,
            canonical_url=url,
            url_normalized=url,
            content_kind="html_page",
            language="en",
            title=title,
            discovery_status="seed",
        )
        session.add(doc)
        session.flush()
        ver_objs = []
        for idx, spec in enumerate(versions_spec, start=1):
            ver = DocumentVersion(
                document_id=doc.id,
                fetched_via_source_id=src.id,
                version_number=idx,
                source_url=url,
                content_type="text/html",
                retrieved_at=spec.get("retrieved_at") or datetime.now(timezone.utc),
                source_published_at=spec.get("published_at"),
                legal_status=spec.get("legal_status", "unknown"),
                http_status=200,
                content_sha256=_sha(f"{url}-v{idx}-{spec['text']}"),
                byte_size=len(spec["text"]),
                storage_path=f"raw/documents/{doc.id}/v{idx}.html",
                is_current=bool(spec.get("is_current", False)),
            )
            session.add(ver)
            session.flush()
            ver_objs.append(ver)
            chunk = DocumentChunk(
                document_id=doc.id,
                document_version_id=ver.id,
                source_id=src.id,
                jurisdiction_id=jur.id,
                jurisdiction_code=jur.code,
                chunk_index=0,
                heading_path=spec.get("heading") or "Salary & Eligibility",
                content_text=spec["text"],
                content_sha256=_sha(spec["text"]),
                is_current_version=bool(spec.get("is_current", False)),
            )
            session.add(chunk)
            session.flush()
            vec = fake_emb.embed_passages([spec["text"]]).vectors[0]
            session.add(
                DocumentChunkEmbedding(
                    chunk_id=chunk.id,
                    embedding_model_id=PLANNED_EMBEDDING_MODEL_ID,
                    embedding=list(map(float, vec.tolist())),
                )
            )
        session.flush()
        return doc, ver_objs

    # 1. Multi-version GB Skilled Worker guidance doc
    doc_sw, sw_vers = _add_doc_versions(
        src_gb,
        jur_gb,
        auth_gb,
        "https://www.gov.uk/skilled-worker-visa",
        "Skilled Worker visa synthetic guidance",
        [
            {
                "is_current": False,
                "legal_status": "historical",
                "published_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
                "text": (
                    "2020 synthetic Skilled Worker rules: general salary threshold "
                    "was £25,600 per year for sponsored roles in 2020."
                ),
            },
            {
                "is_current": False,
                "legal_status": "superseded",
                "published_at": datetime(2024, 4, 4, tzinfo=timezone.utc),
                "text": (
                    "2024 synthetic Skilled Worker rules: general salary threshold "
                    "was £38,700 per year from April 2024."
                ),
            },
            {
                "is_current": True,
                "legal_status": "unknown",
                "published_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                "text": (
                    "Current synthetic Skilled Worker general salary threshold is £41,700 per year. "
                    "For those who held a Tier 2 or Skilled Worker visa before 4 April 2024 and "
                    "have continually held Skilled Worker visas since, the transitional threshold is "
                    "£31,300; these lower requirements apply only to applications made before 4 April 2030. "
                    "Previously in 2018 the certificate fee was £200. "
                    "Until 31 March 2026, the reduced sunset fee was £400. "
                    "Effective from 1 December 2026, the future threshold will be £52,000."
                ),
            },
        ],
    )

    # 2. Archived notice doc (crawl is_current=True, legal_status='archived')
    doc_arch, arch_vers = _add_doc_versions(
        src_gb,
        jur_gb,
        auth_gb,
        "https://www.gov.uk/skilled-worker-visa/archived-2021-notice",
        "Archived 2021 salary notice",
        [
            {
                "is_current": True,
                "legal_status": "archived",
                "published_at": datetime(2021, 5, 1, tzinfo=timezone.utc),
                "text": (
                    "Archived 2021 synthetic Skilled Worker notice: legacy pilot "
                    "remuneration floor was £19,500 in 2021."
                ),
            }
        ],
    )

    # 2b. Proposed & withdrawn docs (non-enacted / non-authoritative statuses)
    doc_prop, _ = _add_doc_versions(
        src_gb,
        jur_gb,
        auth_gb,
        "https://www.gov.uk/skilled-worker-visa/proposed-2027-pilot-grant",
        "Proposed 2027 pilot grant notice",
        [
            {
                "is_current": True,
                "legal_status": "proposed",
                "published_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
                "text": (
                    "Proposed synthetic 2027 pilot relocation grant of £8,500 "
                    "(not yet enacted or in force)."
                ),
            }
        ],
    )
    doc_withdrawn, _ = _add_doc_versions(
        src_gb,
        jur_gb,
        auth_gb,
        "https://www.gov.uk/skilled-worker-visa/withdrawn-stipend",
        "Withdrawn relocation stipend notice",
        [
            {
                "is_current": True,
                "legal_status": "withdrawn",
                "published_at": datetime(2024, 2, 1, tzinfo=timezone.utc),
                "text": (
                    "Withdrawn synthetic relocation stipend rule of £3,250 "
                    "(withdrawn and no longer valid)."
                ),
            }
        ],
    )
    # Isolated CA jurisdiction with ONLY proposed & withdrawn docs to prove E2E abstention
    _add_doc_versions(
        src_ca,
        jur_ca,
        auth_ca,
        "https://www.canada.ca/en/immigration-refugees-citizenship/proposed-pilot",
        "CA Proposed Pilot",
        [
            {
                "is_current": True,
                "legal_status": "proposed",
                "text": "Proposed synthetic CA pilot allowance CAD $9,900 (not enacted).",
            }
        ],
    )
    _add_doc_versions(
        src_ca,
        jur_ca,
        auth_ca,
        "https://www.canada.ca/en/immigration-refugees-citizenship/withdrawn-pilot",
        "CA Withdrawn Pilot",
        [
            {
                "is_current": True,
                "legal_status": "withdrawn",
                "text": "Withdrawn synthetic CA pilot allowance CAD $4,400 (withdrawn).",
            }
        ],
    )

    # 3. IE CSEP and GEP docs for wrong-pathway test
    doc_csep, _ = _add_doc_versions(
        src_ie,
        jur_ie,
        auth_ie,
        "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/permit-types/critical-skills-employment-permit/",
        "IE Critical Skills synthetic",
        [
            {
                "is_current": True,
                "legal_status": "unknown",
                "text": (
                    "Critical Skills Employment Permit criteria depend on occupation "
                    "and proposed remuneration; some occupations require €38,000."
                ),
            }
        ],
    )
    doc_gep, _ = _add_doc_versions(
        src_ie,
        jur_ie,
        auth_ie,
        "https://enterprise.gov.ie/en/what-we-do/workplace-and-skills/employment-permits/permit-types/general-employment-permit/",
        "IE General Employment synthetic",
        [
            {
                "is_current": True,
                "legal_status": "unknown",
                "text": (
                    "General Employment Permit minimum annual remuneration is €68,911 "
                    "for certain non-critical occupations."
                ),
            }
        ],
    )

    now = datetime.now(timezone.utc)
    rules = [
        LegalRule(
            rule_key="syn_gb_sw.general_2026",
            rule_revision=1,
            jurisdiction_id=jur_gb.id,
            jurisdiction_code="GB",
            pathway_id="gb_skilled_worker",
            requirement_category="financial_or_sponsorship",
            requirement_type="salary_threshold",
            numeric_value=Decimal("41700.00"),
            numeric_comparator="gte",
            currency_code="GBP",
            amount_unit="yearly",
            effective_from=date(2025, 7, 22),
            effective_to=date(2026, 11, 30),
            effective_from_inclusive=True,
            effective_to_inclusive=True,
            effective_date_status="known_bounded",
            cohort_key="general",
            cohort_category="general",
            is_transitional=False,
            cohort_conditions={},
            rule_summary="Synthetic current general Skilled Worker salary threshold GBP 41,700.",
            document_id=doc_sw.id,
            document_version_id=sw_vers[2].id,
            source_id=src_gb.id,
            source_url=doc_sw.canonical_url,
            review_status="approved",
            verification_status="human_verified",
            reviewed_by="acceptance_reviewer",
            reviewed_at=now,
        ),
        LegalRule(
            rule_key="syn_gb_sw.transitional_pre2024",
            rule_revision=1,
            jurisdiction_id=jur_gb.id,
            jurisdiction_code="GB",
            pathway_id="gb_skilled_worker",
            requirement_category="financial_or_sponsorship",
            requirement_type="salary_threshold",
            numeric_value=Decimal("31300.00"),
            numeric_comparator="gte",
            currency_code="GBP",
            amount_unit="yearly",
            effective_from=date(2024, 4, 4),
            effective_to=date(2030, 4, 4),
            effective_from_inclusive=True,
            effective_to_inclusive=False,
            effective_date_status="known_bounded",
            cohort_key="transitional_pre2024_cos",
            cohort_category="transitional",
            is_transitional=True,
            cohort_conditions={"prior_permission_before": "2024-04-04", "continuous_status": True},
            rule_summary=(
                "Synthetic transitional Skilled Worker threshold GBP 31,300 for "
                "pre-4 April 2024 CoS holders before 4 April 2030."
            ),
            document_id=doc_sw.id,
            document_version_id=sw_vers[2].id,
            source_id=src_gb.id,
            source_url=doc_sw.canonical_url,
            review_status="approved",
            verification_status="human_verified",
            reviewed_by="acceptance_reviewer",
            reviewed_at=now,
        ),
        LegalRule(
            rule_key="syn_gb_sw.historical_2020",
            rule_revision=1,
            jurisdiction_id=jur_gb.id,
            jurisdiction_code="GB",
            pathway_id="gb_skilled_worker",
            requirement_category="financial_or_sponsorship",
            requirement_type="salary_threshold",
            numeric_value=Decimal("25600.00"),
            numeric_comparator="gte",
            currency_code="GBP",
            amount_unit="yearly",
            effective_from=date(2020, 1, 1),
            effective_to=date(2023, 12, 31),
            effective_from_inclusive=True,
            effective_to_inclusive=True,
            effective_date_status="known_bounded",
            cohort_key="general",
            cohort_category="general",
            is_transitional=False,
            cohort_conditions={},
            rule_summary="Synthetic historical 2020-2023 Skilled Worker threshold GBP 25,600.",
            document_id=doc_sw.id,
            document_version_id=sw_vers[0].id,
            source_id=src_gb.id,
            source_url=doc_sw.canonical_url,
            review_status="approved",
            verification_status="human_verified",
            reviewed_by="acceptance_reviewer",
            reviewed_at=now,
        ),
        LegalRule(
            rule_key="syn_gb_sw.future_dec2026",
            rule_revision=1,
            jurisdiction_id=jur_gb.id,
            jurisdiction_code="GB",
            pathway_id="gb_skilled_worker",
            requirement_category="financial_or_sponsorship",
            requirement_type="salary_threshold",
            numeric_value=Decimal("52000.00"),
            numeric_comparator="gte",
            currency_code="GBP",
            amount_unit="yearly",
            effective_from=date(2026, 12, 1),
            effective_to=None,
            effective_from_inclusive=True,
            effective_to_inclusive=True,
            effective_date_status="announced_future",
            cohort_key="general",
            cohort_category="general",
            is_transitional=False,
            cohort_conditions={},
            rule_summary="Synthetic future Skilled Worker threshold GBP 52,000 from 2026-12-01.",
            document_id=doc_sw.id,
            document_version_id=sw_vers[2].id,
            source_id=src_gb.id,
            source_url=doc_sw.canonical_url,
            review_status="approved",
            verification_status="human_verified",
            reviewed_by="acceptance_reviewer",
            reviewed_at=now,
        ),
        LegalRule(
            rule_key="syn_gb_sw.unreviewed_draft",
            rule_revision=1,
            jurisdiction_id=jur_gb.id,
            jurisdiction_code="GB",
            pathway_id="gb_skilled_worker",
            requirement_category="financial_or_sponsorship",
            requirement_type="salary_threshold",
            numeric_value=Decimal("11111.00"),
            numeric_comparator="gte",
            currency_code="GBP",
            amount_unit="yearly",
            effective_from=date(2025, 1, 1),
            effective_to=None,
            effective_date_status="known_open_ended",
            cohort_key="general",
            cohort_category="general",
            is_transitional=False,
            rule_summary="Unreviewed synthetic draft GBP 11,111.",
            document_id=doc_sw.id,
            document_version_id=sw_vers[2].id,
            source_id=src_gb.id,
            review_status="unreviewed",
            verification_status="unverified",
        ),
        LegalRule(
            rule_key="syn_gb_sw.unknown_dates",
            rule_revision=1,
            jurisdiction_id=jur_gb.id,
            jurisdiction_code="GB",
            pathway_id="gb_skilled_worker",
            requirement_category="financial_or_sponsorship",
            requirement_type="salary_threshold",
            numeric_value=Decimal("77777.00"),
            numeric_comparator="gte",
            currency_code="GBP",
            amount_unit="yearly",
            effective_from=None,
            effective_to=None,
            effective_date_status="unknown",
            cohort_key="general",
            cohort_category="general",
            is_transitional=False,
            rule_summary="Unknown-effective-date synthetic rule GBP 77,777.",
            document_id=doc_sw.id,
            document_version_id=sw_vers[2].id,
            source_id=src_gb.id,
            review_status="approved",
            verification_status="human_verified",
            reviewed_by="acceptance_reviewer",
            reviewed_at=now,
        ),
        # Conflicting reviewed rules in isolated test pathway for Step 2
        LegalRule(
            rule_key="syn_gb_conflict.rule_a",
            rule_revision=1,
            jurisdiction_id=jur_gb.id,
            jurisdiction_code="GB",
            pathway_id="gb_conflict_test",
            requirement_category="financial_or_sponsorship",
            requirement_type="salary_threshold",
            numeric_value=Decimal("30000.00"),
            numeric_comparator="gte",
            currency_code="GBP",
            amount_unit="yearly",
            effective_from=date(2025, 1, 1),
            effective_to=None,
            effective_date_status="known_open_ended",
            cohort_key="general",
            cohort_category="general",
            is_transitional=False,
            rule_summary="Conflicting synthetic rule A GBP 30,000.",
            document_id=doc_sw.id,
            document_version_id=sw_vers[2].id,
            source_id=src_gb.id,
            review_status="approved",
            verification_status="human_verified",
            reviewed_by="acceptance_reviewer",
            reviewed_at=now,
        ),
        LegalRule(
            rule_key="syn_gb_conflict.rule_b",
            rule_revision=1,
            jurisdiction_id=jur_gb.id,
            jurisdiction_code="GB",
            pathway_id="gb_conflict_test",
            requirement_category="financial_or_sponsorship",
            requirement_type="salary_threshold",
            numeric_value=Decimal("35000.00"),
            numeric_comparator="gte",
            currency_code="GBP",
            amount_unit="yearly",
            effective_from=date(2025, 1, 1),
            effective_to=None,
            effective_date_status="known_open_ended",
            cohort_key="general",
            cohort_category="general",
            is_transitional=False,
            rule_summary="Conflicting synthetic rule B GBP 35,000.",
            document_id=doc_sw.id,
            document_version_id=sw_vers[2].id,
            source_id=src_gb.id,
            review_status="approved",
            verification_status="human_verified",
            reviewed_by="acceptance_reviewer",
            reviewed_at=now,
        ),
    ]
    # Insert as unverified first (10D.4 trigger); then promote via synthetic authority.
    for rule in rules:
        if rule.verification_status == 'human_verified':
            rule.verification_status = 'unverified'
            rule.review_status = 'unreviewed'
            rule.reviewed_by = None
            rule.reviewed_at = None
    session.add_all(rules)
    session.flush()
    from lifepilot.governance.synthetic_authority import ensure_synthetic_human_authority
    for rule in rules:
        if rule.rule_key != 'syn_gb_sw.unreviewed_draft':
            ensure_synthetic_human_authority(
                session, rule, actor_id='acceptance_reviewer'
            )

    return {
        "embedder": fake_emb,
        "jur_gb": jur_gb,
        "src_gb": src_gb,
        "doc_sw": doc_sw,
        "sw_vers": sw_vers,
        "doc_arch": doc_arch,
        "doc_prop": doc_prop,
        "doc_withdrawn": doc_withdrawn,
        "doc_csep": doc_csep,
        "doc_gep": doc_gep,
    }


@pytest.fixture()
def _e2e_env(db_session: Session, reset_agent_runtime, monkeypatch):
    data = _seed_controlled_dataset(db_session)
    external_calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        external_calls.append(str(request.url))
        return httpx.Response(404, request=request)

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
    def _bound():
        yield db_session

    orch = EvidenceOrchestrator(
        session_factory=_bound,
        embedder=data["embedder"],
        external_router=router,
    )
    set_evidence_orchestrator_override(orch)

    monkeypatch.setenv("LIFEPILOT_ENV", "development")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_AUTH", "1")
    monkeypatch.setenv("LIFEPILOT_API_KEYS", "accept-key")
    monkeypatch.setenv("LIFEPILOT_VERIFICATION_ENABLED", "1")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_LLM", "0")
    reset_api_settings_cache()
    reset_agent_config_cache()
    reset_rate_limit_state()

    yield {**data, "orch": orch, "external_calls": external_calls}

    reset_api_settings_cache()
    reset_agent_config_cache()
    reset_rate_limit_state()


# ---------------------------------------------------------------------------
# End-to-End Integration Scenarios 1–20
# ---------------------------------------------------------------------------


def test_e2e_current_historical_nl_year_only_and_ambiguous_api_paths(_e2e_env):
    import lifepilot.api.app as api_app

    def _build_answer(prompt: str) -> GroundedAnswer:
        eids = _extract_eids_from_prompt(prompt)
        rule_eid = next((eid for eid, blk in eids if "curated_rule" in blk), eids[0][0])
        passage_eid = next(
            (eid for eid, blk in eids if "internal_rag" in blk), rule_eid
        )
        if "25,600" in prompt or "25600" in prompt:
            return GroundedAnswer(
                answer="In 2020-2023, the historical synthetic threshold was GBP 25,600.",
                claims=[
                    AnswerClaim(
                        claim="Historical synthetic threshold GBP 25,600 applied until 2023-12-31.",
                        evidence_ids=[rule_eid],
                    )
                ],
            )
        return GroundedAnswer(
            answer="The general synthetic threshold is GBP 41,700.",
            claims=[
                AnswerClaim(
                    claim="Synthetic current general Skilled Worker salary threshold is GBP 41,700.",
                    evidence_ids=[rule_eid, passage_eid],
                )
            ],
        )

    set_llm_provider_override(_ScenarioFakeLLM(_build_answer))
    client = TestClient(api_app.create_app())
    headers = {"X-API-Key": "accept-key"}

    # Scenario 1 & 20: Current-law question without as_of_date (legacy API compatible)
    r_cur = client.post(
        "/v1/answer",
        headers=headers,
        json={
            "question": "What is the current Skilled Worker general salary threshold?",
            "jurisdiction": "GB",
            "pathway_id": "gb_skilled_worker",
        },
    )
    assert r_cur.status_code == 200
    b_cur = r_cur.json()
    assert b_cur["temporal_mode"] == "current"
    assert b_cur["temporal_outcome"] == TemporalOutcomeStatus.REVIEWED_RULE_FOUND.value
    # Multi-rule co-cited chunk must not falsely downgrade current reviewed rule
    assert b_cur["verification"]["claim_outcomes"][0]["status"] == "supported"
    assert "41,700" in (b_cur["answer"] or "")
    # Scenario 14: Archived, proposed, and withdrawn docs excluded from current-law evidence
    assert "19,500" not in (b_cur["answer"] or "")
    assert "8,500" not in (b_cur["answer"] or "")
    assert "3,250" not in (b_cur["answer"] or "")
    # Scenario 15: Unreviewed rule (11,111) and unknown-date rule (77,777) excluded
    assert "11,111" not in (b_cur["answer"] or "")
    assert "77,777" not in (b_cur["answer"] or "")
    # Same-year future rule (52,000 from 2026-12-01) is not active on 2026-11-18
    assert "52,000" not in (b_cur["answer"] or "")

    # Scenario 2 & 8 & 19: Historical explicit as_of_date (expired 2020-2023 rule valid in 2022)
    # External current-page connectors must NOT be called for historical GB query!
    _e2e_env["external_calls"].clear()
    r_hist = client.post(
        "/v1/answer",
        headers=headers,
        json={
            "question": "What was the Skilled Worker salary threshold?",
            "jurisdiction": "GB",
            "pathway_id": "gb_skilled_worker",
            "as_of_date": "2022-06-01",
        },
    )
    assert r_hist.status_code == 200
    b_hist = r_hist.json()
    assert _e2e_env["external_calls"] == []
    assert b_hist["as_of_date"] == "2022-06-01"
    assert b_hist["resolved_reference_date"] == "2022-06-01"
    assert b_hist["temporal_mode"] == "historical"
    assert b_hist["temporal_outcome"] == TemporalOutcomeStatus.REVIEWED_RULE_FOUND.value
    assert "25,600" in (b_hist["answer"] or "")

    # Scenario 3: Exact historical date in natural language (omitted as_of_date)
    r_nl = client.post(
        "/v1/answer",
        headers=headers,
        json={
            "question": "What salary threshold applied on 15 March 2022?",
            "jurisdiction": "GB",
            "pathway_id": "gb_skilled_worker",
        },
    )
    assert r_nl.status_code == 200
    b_nl = r_nl.json()
    assert b_nl["as_of_date"] is None
    assert b_nl["resolved_reference_date"] == "2022-03-15"
    assert b_nl["temporal_mode"] == "historical"
    assert "25,600" in (b_nl["answer"] or "")

    # Scenario 4: Year-only historical question does NOT invent Jan 1 / Dec 31
    r_yr = client.post(
        "/v1/answer",
        headers=headers,
        json={
            "question": "What was the Skilled Worker salary threshold in 2020?",
            "jurisdiction": "GB",
            "pathway_id": "gb_skilled_worker",
        },
    )
    assert r_yr.status_code == 200
    b_yr = r_yr.json()
    assert b_yr["resolved_reference_date"] is None
    assert b_yr["temporal_mode"] == "historical"
    assert "exact_as_of_date_within_period:2020" in b_yr["missing_information"]

    # Scenario 5: Ambiguous numeric date requests clarification & abstains
    r_amb = client.post(
        "/v1/answer",
        headers=headers,
        json={
            "question": "What salary threshold applied on 04/05/2022?",
            "jurisdiction": "GB",
            "pathway_id": "gb_skilled_worker",
        },
    )
    assert r_amb.status_code == 200
    b_amb = r_amb.json()
    assert b_amb["status"] == "insufficient_evidence"
    assert b_amb["answer"] is None
    assert "unambiguous_as_of_date_YYYY_MM_DD" in b_amb["missing_information"]

    # Scenario 19 (explicit external_mode="on" on unsupported 2015 historical query):
    # Current-page external connectors must still be blocked and abstention preserved.
    _e2e_env["external_calls"].clear()
    r_hist_ext_on = run_agent(
        AgentRequest(
            question="What was the Skilled Worker salary threshold in 2015?",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            as_of_date=date(2015, 6, 1),
            retrieval={"external_mode": "on"},
        ),
        config=AgentConfig(llm_provider="fake", llm_model="fake", verification_enabled=True),
    )
    assert _e2e_env["external_calls"] == []
    assert r_hist_ext_on.status == AgentStatus.INSUFFICIENT_EVIDENCE
    assert r_hist_ext_on.temporal_outcome == TemporalOutcomeStatus.CURRENT_AVAILABLE_HISTORICAL_UNAVAILABLE.value
    assert r_hist_ext_on.retrieval_decision == "internal_only"

    # Explicit future as_of_date (2026-12-15 >= 2026-12-01) picks up future_dec2026
    def _build_future(prompt: str) -> GroundedAnswer:
        eids = _extract_eids_from_prompt(prompt)
        fut_eid = next((eid for eid, b in eids if "52000" in b or "52,000" in b), eids[0][0])
        return GroundedAnswer(
            answer="From 1 December 2026, the synthetic threshold is GBP 52,000.",
            claims=[
                AnswerClaim(
                    claim="Synthetic future Skilled Worker threshold GBP 52,000 applies from 2026-12-01.",
                    evidence_ids=[fut_eid],
                )
            ],
        )

    set_llm_provider_override(_ScenarioFakeLLM(_build_future))
    r_fut = client.post(
        "/v1/answer",
        headers=headers,
        json={
            "question": "What Skilled Worker salary threshold applies from 15 December 2026?",
            "jurisdiction": "GB",
            "pathway_id": "gb_skilled_worker",
            "as_of_date": "2026-12-15",
        },
    )
    assert r_fut.status_code == 200
    b_fut = r_fut.json()
    assert b_fut["temporal_mode"] == "future"
    assert b_fut["temporal_outcome"] == TemporalOutcomeStatus.REVIEWED_RULE_FOUND.value
    assert "52,000" in (b_fut["answer"] or "")


def test_e2e_transitional_cohorts_and_same_scope_contradictions(_e2e_env):
    # Scenario 9 & 10: General + transitional coexisting when cohort confirmed
    def _build_trans(prompt: str) -> GroundedAnswer:
        eids = _extract_eids_from_prompt(prompt)
        gen_eid = next((eid for eid, b in eids if "41700" in b or "41,700" in b), eids[0][0])
        trans_eid = next((eid for eid, b in eids if "31300" in b or "31,300" in b), eids[0][0])
        return GroundedAnswer(
            answer=(
                "General synthetic threshold is £41,700. For pre-4 April 2024 CoS holders, "
                "transitional threshold is £31,300 and applies only before 4 April 2030."
            ),
            claims=[
                AnswerClaim(
                    claim="Synthetic current general Skilled Worker salary threshold is £41,700.",
                    evidence_ids=[gen_eid],
                ),
                AnswerClaim(
                    claim=(
                        "For pre-4 April 2024 CoS holders with continuous status, the "
                        "transitional threshold is £31,300 and applies only to "
                        "applications made before 4 April 2030."
                    ),
                    evidence_ids=[trans_eid],
                ),
            ],
        )

    llm = _ScenarioFakeLLM(_build_trans)
    set_llm_provider_override(llm)
    resp = run_agent(
        AgentRequest(
            question="What Skilled Worker salary applies to me?",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            user_circumstances={
                "prior_permission_date": "2023-05-01",
                "continuous_status": True,
            },
            retrieval={"external_mode": "off"},
        ),
        config=AgentConfig(llm_provider="fake", llm_model="fake", verification_enabled=True),
    )
    assert resp.status == AgentStatus.SUCCESS
    assert len(resp.claims) == 2
    assert "41,700" in (resp.answer or "")
    assert "31,300" in (resp.answer or "")
    assert "before 4 April 2030" in (resp.answer or "")

    # Post-April-2024 applicant cohort must NOT receive pre-2024 transitional LegalRule
    orch_post = _e2e_env["orch"].gather(
        question="What Skilled Worker salary applies to me?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        user_circumstances={
            "prior_permission_date": "2025-02-01",
            "continuous_status": True,
        },
        external_mode="off",
    )
    post_curated_keys = [
        (e.temporal_provenance or {}).get("rule_key")
        for e in orch_post.evidence
        if e.source_type == EvidenceSourceType.CURATED_RULE
    ]
    assert "syn_gb_sw.general_2026" in post_curated_keys
    assert "syn_gb_sw.transitional_pre2024" not in post_curated_keys

    def _build_general_only(prompt: str) -> GroundedAnswer:
        eids = _extract_eids_from_prompt(prompt)
        gen_eid = next((eid for eid, b in eids if "curated_rule" in b and "41700" in b), eids[0][0])
        return GroundedAnswer(
            answer="General synthetic threshold is £41,700.",
            claims=[
                AnswerClaim(
                    claim="Synthetic current general Skilled Worker salary threshold is £41,700.",
                    evidence_ids=[gen_eid],
                )
            ],
        )

    set_llm_provider_override(_ScenarioFakeLLM(_build_general_only))
    resp_post2024 = run_agent(
        AgentRequest(
            question="What Skilled Worker salary applies to me?",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            user_circumstances={
                "prior_permission_date": "2025-02-01",
                "continuous_status": True,
            },
            retrieval={"external_mode": "off"},
        ),
        config=AgentConfig(llm_provider="fake", llm_model="fake", verification_enabled=True),
    )
    assert resp_post2024.status == AgentStatus.SUCCESS
    assert "41,700" in (resp_post2024.answer or "")
    assert "31,300" not in (resp_post2024.answer or "")

    # Conflicting reviewed rules for same cohort and period -> CONFLICTING_TEMPORAL_EVIDENCE
    def _build_conflict_ans(prompt: str) -> GroundedAnswer:
        eids = _extract_eids_from_prompt(prompt)
        return GroundedAnswer(
            answer="A rule lists GBP 30,000.",
            claims=[
                AnswerClaim(
                    claim="Conflicting synthetic rule A GBP 30,000.",
                    evidence_ids=[eids[0][0]],
                )
            ],
        )

    set_llm_provider_override(_ScenarioFakeLLM(_build_conflict_ans))
    resp_conf = run_agent(
        AgentRequest(
            question="What is the gb_conflict_test threshold?",
            jurisdiction="GB",
            pathway_id="gb_conflict_test",
            retrieval={"external_mode": "off"},
        ),
        config=AgentConfig(llm_provider="fake", llm_model="fake", verification_enabled=True),
    )
    assert resp_conf.temporal_outcome == TemporalOutcomeStatus.CONFLICTING_TEMPORAL_EVIDENCE.value

    # Scenario 11 & 18: Genuine same-scope contradiction vs reviewed rule + zero leakage into prose
    def _build_bad(prompt: str) -> GroundedAnswer:
        eids = _extract_eids_from_prompt(prompt)
        passage_eid = next((eid for eid, b in eids if "internal_rag" in b), eids[0][0])
        return GroundedAnswer(
            answer="The general salary threshold is £38,700 per year. Unpublished bonus is £66,666.",
            claims=[
                AnswerClaim(
                    claim="The Skilled Worker general salary threshold is £38,700 per year.",
                    evidence_ids=[passage_eid],
                ),
                AnswerClaim(
                    claim="There is also an unpublished £66,666 requirement.",
                    evidence_ids=[passage_eid],
                ),
            ],
        )

    set_llm_provider_override(_ScenarioFakeLLM(_build_bad))
    resp_bad = run_agent(
        AgentRequest(
            question="What is the general Skilled Worker salary?",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            retrieval={"external_mode": "off"},
        ),
        config=AgentConfig(llm_provider="fake", llm_model="fake", verification_enabled=True),
    )
    assert "38,700" not in (resp_bad.answer or "")
    assert "66,666" not in (resp_bad.answer or "")
    assert resp_bad.status == AgentStatus.INSUFFICIENT_EVIDENCE


def test_e2e_missing_historical_current_old_rule_archived_and_wrong_pathway(_e2e_env):
    # Scenario 16: Missing historical evidence (2015) abstains cleanly with 0 LLM calls
    llm = _ScenarioFakeLLM(
        lambda _p: (_ for _ in ()).throw(AssertionError("LLM must not run when evidence insufficient"))
    )
    set_llm_provider_override(llm)
    resp_missing = run_agent(
        AgentRequest(
            question="What was the Skilled Worker salary threshold in 2015?",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            as_of_date=date(2015, 6, 1),
            retrieval={"external_mode": "off"},
        ),
        config=AgentConfig(llm_provider="fake", llm_model="fake", verification_enabled=True),
    )
    assert llm.calls == 0
    assert resp_missing.status == AgentStatus.INSUFFICIENT_EVIDENCE
    assert resp_missing.temporal_outcome == TemporalOutcomeStatus.CURRENT_AVAILABLE_HISTORICAL_UNAVAILABLE.value

    # Scenario 12: Current document describing an old rule ("In 2018 the fee was £200")
    def _build_2018(prompt: str) -> GroundedAnswer:
        eids = _extract_eids_from_prompt(prompt)
        return GroundedAnswer(
            answer="In 2018 the synthetic certificate fee was £200.",
            claims=[
                AnswerClaim(
                    claim="Previously in 2018 the certificate fee was £200.",
                    evidence_ids=[eids[0][0]],
                )
            ],
        )

    set_llm_provider_override(_ScenarioFakeLLM(_build_2018))
    resp_2018 = run_agent(
        AgentRequest(
            question="What was the fee on 1 June 2018?",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            as_of_date=date(2018, 6, 1),
            retrieval={"external_mode": "off"},
        ),
        config=AgentConfig(llm_provider="fake", llm_model="fake", verification_enabled=True),
    )
    assert resp_2018.status == AgentStatus.SUCCESS
    assert resp_2018.temporal_outcome == TemporalOutcomeStatus.HISTORICAL_CANDIDATE_UNVERIFIED.value
    assert resp_2018.verification["claim_outcomes"][0]["status"] == "temporal_uncertainty"

    # Scenario 13: Archived document retrieved as historical candidate on 2021-06-01
    def _build_arch(prompt: str) -> GroundedAnswer:
        eids = _extract_eids_from_prompt(prompt)
        arch_eid = next((eid for eid, b in eids if "19,500" in b), eids[0][0])
        return GroundedAnswer(
            answer="The archived 2021 notice stated a floor of £19,500.",
            claims=[
                AnswerClaim(
                    claim="Archived 2021 synthetic Skilled Worker notice listed a legacy pilot remuneration floor of £19,500 in 2021.",
                    evidence_ids=[arch_eid],
                )
            ],
        )

    set_llm_provider_override(_ScenarioFakeLLM(_build_arch))
    resp_arch = run_agent(
        AgentRequest(
            question="legacy pilot remuneration floor 2021",
            jurisdiction="GB",
            pathway_id="gb_legacy_pilot",
            as_of_date=date(2021, 6, 1),
            retrieval={"external_mode": "off"},
        ),
        config=AgentConfig(llm_provider="fake", llm_model="fake", verification_enabled=True),
    )
    assert resp_arch.status == AgentStatus.SUCCESS
    assert resp_arch.temporal_outcome == TemporalOutcomeStatus.HISTORICAL_CANDIDATE_UNVERIFIED.value
    assert "19,500" in (resp_arch.answer or "")
    assert "not human-verified" in (resp_arch.answer or "").lower()

    # Scenario 17: Wrong-pathway IE GEP €68,911 attributed to CSEP is suppressed cleanly
    def _build_wrong_ie(prompt: str) -> GroundedAnswer:
        eids = _extract_eids_from_prompt(prompt)
        gep_eid = next((eid for eid, b in eids if "68,911" in b), eids[0][0])
        return GroundedAnswer(
            answer="The Critical Skills Employment Permit minimum is €68,911.",
            claims=[
                AnswerClaim(
                    claim="The Critical Skills Employment Permit minimum annual remuneration is €68,911.",
                    evidence_ids=[gep_eid],
                )
            ],
        )

    set_llm_provider_override(_ScenarioFakeLLM(_build_wrong_ie))
    resp_ie = run_agent(
        AgentRequest(
            question="What remuneration applies to Critical Skills Employment Permit?",
            jurisdiction="IE",
            pathway_id="ie_critical_skills_employment_permit",
            retrieval={"external_mode": "off"},
        ),
        config=AgentConfig(llm_provider="fake", llm_model="fake", verification_enabled=True),
    )
    assert resp_ie.status == AgentStatus.INSUFFICIENT_EVIDENCE
    assert "68,911" not in (resp_ie.answer or "")

    # Proposed / withdrawn documents in GB are filtered out of kept evidence
    orch_gb_non = _e2e_env["orch"].gather(
        question="Proposed synthetic 2027 pilot relocation grant withdrawn stipend",
        jurisdiction="GB",
        external_mode="off",
    )
    assert all("8,500" not in (e.passage_text or "") and "3,250" not in (e.passage_text or "") for e in orch_gb_non.evidence)
    excluded_reasons = {x["reason"] for x in orch_gb_non.excluded_evidence}
    assert any("proposed" in r or "withdrawn" in r for r in excluded_reasons)

    # Isolated jurisdiction with ONLY proposed/withdrawn docs -> 0 LLM calls, EXCLUDED_NON_APPLICABLE_STATUS
    llm_no_call = _ScenarioFakeLLM(
        lambda _p: (_ for _ in ()).throw(AssertionError("LLM must not run on excluded statuses"))
    )
    set_llm_provider_override(llm_no_call)
    resp_ca = run_agent(
        AgentRequest(
            question="Proposed or withdrawn CA pilot allowance",
            jurisdiction="CA",
            retrieval={"external_mode": "off"},
        ),
        config=AgentConfig(llm_provider="fake", llm_model="fake", verification_enabled=True),
    )
    assert llm_no_call.calls == 0
    assert resp_ca.status == AgentStatus.INSUFFICIENT_EVIDENCE
    assert resp_ca.temporal_outcome == TemporalOutcomeStatus.EXCLUDED_NON_APPLICABLE_STATUS.value
    assert "9,900" not in (resp_ca.answer or "")
    assert "4,400" not in (resp_ca.answer or "")

    # Scenario 6 & 7 full E2E: Legitimate current + future + expired-framed claims coexist safely
    def _build_mixed_temporal(prompt: str) -> GroundedAnswer:
        eids = _extract_eids_from_prompt(prompt)
        rule_eid = next((eid for eid, b in eids if "curated_rule" in b and "41700" in b), eids[0][0])
        passage_eid = next((eid for eid, b in eids if "internal_rag" in b and "52,000" in b), rule_eid)
        return GroundedAnswer(
            answer=(
                "The current general salary threshold is £41,700. "
                "Until 31 March 2026, the reduced sunset fee was £400. "
                "Effective from 1 December 2026, the future threshold will be £52,000."
            ),
            claims=[
                AnswerClaim(
                    claim="Synthetic current general Skilled Worker salary threshold is £41,700.",
                    evidence_ids=[rule_eid],
                ),
                AnswerClaim(
                    claim="Until 31 March 2026, the reduced sunset fee was £400.",
                    evidence_ids=[passage_eid],
                ),
                AnswerClaim(
                    claim="Effective from 1 December 2026, the future threshold will be £52,000.",
                    evidence_ids=[passage_eid],
                ),
            ],
        )

    set_llm_provider_override(_ScenarioFakeLLM(_build_mixed_temporal))
    resp_mix = run_agent(
        AgentRequest(
            question="What is the current Skilled Worker salary threshold and upcoming or ended changes?",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            retrieval={"external_mode": "off"},
        ),
        config=AgentConfig(llm_provider="fake", llm_model="fake", verification_enabled=True),
    )
    assert resp_mix.status == AgentStatus.SUCCESS
    assert len(resp_mix.claims) == 3
    assert resp_mix.verification["claim_outcomes"][0]["status"] == "supported"
    assert resp_mix.verification["claim_outcomes"][1]["status"] == "supported"
    assert resp_mix.verification["claim_outcomes"][2]["status"] == "temporal_uncertainty"
    assert "41,700" in (resp_mix.answer or "")
    assert "Until 31 March 2026" in (resp_mix.answer or "")
    assert "1 December 2026" in (resp_mix.answer or "")
    assert "NOT confirmed as currently applicable" in (resp_mix.answer or "")


def test_step4_future_and_expired_uncertainty_cannot_become_unqualified_current_law():
    """Step 4 regression: even if LLM populates soft uncertainty, temporal caveat must appear."""
    # Scenario 6: Same-year future-effective rule (2026-12-01 queried on 2026-11-18)
    ev_future = evidence_from_internal_passage(
        jurisdiction="GB",
        official_url="https://www.gov.uk/skilled-worker-visa",
        document_id=9001,
        document_version_id=9002,
        chunk_id=1,
        content_text="Effective from 1 December 2026, the general threshold will be £52,000.",
        legal_status="future_pending",
    )
    c_future_as_cur = AnswerClaim(
        claim="The current general salary threshold is £52,000.",
        evidence_ids=[ev_future.evidence_id],
        # Non-empty soft uncertainty previously masked temporal warning!
        uncertainty="Verify occupation code.",
    )
    res_f = verify_material_claims(
        answer=c_future_as_cur.claim,
        claims=[c_future_as_cur],
        evidence=[ev_future],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        resolved_reference_date=date(2026, 11, 18),
        temporal_mode="current",
    )
    assert res_f.outcomes[0].status == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY
    assert "NOT confirmed as currently applicable on 2026-11-18" in (res_f.answer or "")
    assert "Verify occupation code" in (res_f.answer or "")

    # Scenario 7: Expired rule queried as current law (until 31 March 2026 on 2026-11-18)
    ev_expired = evidence_from_internal_passage(
        jurisdiction="GB",
        official_url="https://www.gov.uk/skilled-worker-visa",
        document_id=9003,
        document_version_id=9004,
        chunk_id=2,
        content_text="Until 31 March 2026, the reduced synthetic fee was £400.",
    )
    c_expired_as_cur = AnswerClaim(
        claim="The reduced synthetic fee is £400.",
        evidence_ids=[ev_expired.evidence_id],
        uncertainty="Check location.",
    )
    res_e = verify_material_claims(
        answer=c_expired_as_cur.claim,
        claims=[c_expired_as_cur],
        evidence=[ev_expired],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        resolved_reference_date=date(2026, 11, 18),
        temporal_mode="current",
    )
    assert res_e.outcomes[0].status == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY
    assert "Expired or historical requirement — NOT confirmed as currently applicable on 2026-11-18" in (
        res_e.answer or ""
    )

    # Legitimate upcoming change discussion with explicit future framing remains preserved cleanly
    c_legit_future = AnswerClaim(
        claim="Effective from 1 December 2026, the general threshold will be £52,000.",
        evidence_ids=[ev_future.evidence_id],
    )
    res_legit = verify_material_claims(
        answer=c_legit_future.claim,
        claims=[c_legit_future],
        evidence=[ev_future],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        resolved_reference_date=date(2026, 11, 18),
        temporal_mode="current",
    )
    assert res_legit.outcomes[0].retained is True
    assert "1 December 2026" in (res_legit.answer or "")
    assert "NOT confirmed as currently applicable" in (res_legit.answer or "")

    # Past effective-from date ("From 22 July 2025") on 2026-11-18 must NOT trigger false future warning
    ev_past_start = evidence_from_internal_passage(
        jurisdiction="GB",
        official_url="https://www.gov.uk/skilled-worker-visa",
        document_id=9005,
        document_version_id=9006,
        chunk_id=3,
        content_text="From 22 July 2025, the general salary threshold is £41,700.",
    )
    c_past_start = AnswerClaim(
        claim="From 22 July 2025, the Skilled Worker general salary threshold is £41,700.",
        evidence_ids=[ev_past_start.evidence_id],
    )
    res_past = verify_material_claims(
        answer=c_past_start.claim,
        claims=[c_past_start],
        evidence=[ev_past_start],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        resolved_reference_date=date(2026, 11, 18),
        temporal_mode="current",
    )
    assert res_past.outcomes[0].status == ClaimVerificationStatus.SUPPORTED
