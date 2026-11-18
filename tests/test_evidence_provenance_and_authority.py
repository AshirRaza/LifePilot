"""Phase 10C.2 Evidence Provenance, Source Authority & Legacy Remediation Tests.

Covers Parts B–H: full DB->EvidenceItem lineage, authority taxonomy,
statistical/portal/proposed/EU safeguards, passage truncation & table headers,
language/locale propagation (no jurisdiction guessing), consolidated pathway
attribution, legacy fixes, API compatibility, and zero extra LLM calls.

All synthetic rules/amounts are strictly labelled synthetic. Live DB untouched.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
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
    evidence_locale_hint,
)
from lifepilot.agent.orchestration import (
    EvidenceOrchestrator,
    OrchestrationResult,
    set_evidence_orchestrator_override,
)
from lifepilot.agent.pathway_applicability import (
    PathwayApplicability,
    classify_pathway_applicability,
    pathway_ids_from_url,
)
from lifepilot.agent.provenance import (
    SourceAuthorityCategory,
    classify_source_authority,
    locale_hint_from_language,
    normalize_language_tag,
)
from lifepilot.agent.providers import (
    LLMUsage,
    StructuredLLMResult,
    set_llm_provider_override,
)
from lifepilot.agent.runner import run_agent
from lifepilot.agent.temporal_selection import (
    TemporalQueryMode,
    select_temporal_evidence,
)
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
    Source,
)
from lifepilot.external.cache import ResponseCache
from lifepilot.external.http import ExternalHttpClient, HttpClientConfig, HttpResponse
from lifepilot.external.models import ExternalQuery, ProviderId, SourceType
from lifepilot.external.providers.canada_open import CanadaOpenConnector
from lifepilot.external.providers.eu_immigration_portal import EuImmigrationPortalConnector
from lifepilot.external.providers.eu_opendata import EuOpenDataConnector
from lifepilot.external.providers.eurlex_cellar import EurlexCellarConnector
from lifepilot.external.providers.govuk import GovukContentConnector
from lifepilot.external.providers.national_official import NationalOfficialConnector
from lifepilot.external.providers.us_federal_register import UsFederalRegisterConnector
from lifepilot.external.textutil import (
    dig_body_html,
    extract_passages_with_truncation,
    html_to_text,
)
from lifepilot.extract.html_structured import (
    extract_html_document,
    extract_html_language_from_html,
)
from lifepilot.extract.schema import normalized_content_sha256
from lifepilot.rag.chunker import E5_SAFE_MAX_CHARS, chunk_structured_document
from lifepilot.rag.embedder import FakeEmbedder
from lifepilot.rag.retrieval import InternalKnowledgeRetriever, InternalQuery


def _sha(val: str) -> str:
    return hashlib.sha256(val.encode("utf-8")).hexdigest()


def _mock_http(handler) -> ExternalHttpClient:
    transport = httpx.MockTransport(handler)
    raw = httpx.Client(transport=transport, follow_redirects=True)
    return ExternalHttpClient(
        config=HttpClientConfig(timeout_seconds=5.0, max_retries=1, backoff_base_seconds=0.01),
        cache=ResponseCache(default_ttl_seconds=60.0, disk_dir=None),
        client=raw,
        allowed_hosts=None,
    )


class _CountingFakeLLM:
    provider_id = "fake"
    model_id = "fake-10c2"

    def __init__(self, builder) -> None:
        self._builder = builder
        self.calls = 0

    def complete(self, *a, **k):
        raise AssertionError("complete() must not be called")

    def complete_structured(self, messages, *, schema, max_tokens, temperature=0.0):
        self.calls += 1
        val = self._builder(messages[-1].content)
        return StructuredLLMResult(
            value=val,
            usage=LLMUsage(input_tokens=40, output_tokens=30, total_tokens=70),
            latency_ms=1.0,
            model=self.model_id,
            provider=self.provider_id,
        )


# ---------------------------------------------------------------------------
# 1–3 & 14: Internal DB -> Retriever -> EvidenceItem lineage, offsets, versions
# ---------------------------------------------------------------------------


def test_internal_lineage_offsets_authority_and_historical_versions(db_session: Session):
    emb = FakeEmbedder()
    jur_nl = Jurisdiction(code="NL", name="Netherlands", jurisdiction_type="country", country_code="NL")
    db_session.add(jur_nl)
    db_session.flush()

    auth_ind = Authority(
        jurisdiction_id=jur_nl.id,
        name="Immigration and Naturalisation Service (IND)",
        short_name="IND",
        website_url="https://ind.nl",
    )
    db_session.add(auth_ind)
    db_session.flush()

    src_primary = Source(
        authority_id=auth_ind.id,
        jurisdiction_id=jur_nl.id,
        name="IND Official Portal",
        seed_url="https://ind.nl/en/residence-permits/work",
        seed_url_normalized="https://ind.nl/en/residence-permits/work",
        review_status="approved",
        default_language="en",
    )
    src_fetcher = Source(
        authority_id=auth_ind.id,
        jurisdiction_id=jur_nl.id,
        name="IND Forms Crawler",
        seed_url="https://ind.nl/en/forms",
        seed_url_normalized="https://ind.nl/en/forms",
        review_status="approved",
        default_language="nl",
    )
    db_session.add_all([src_primary, src_fetcher])
    db_session.flush()

    doc = Document(
        authority_id=auth_ind.id,
        primary_source_id=src_primary.id,
        canonical_url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant",
        url_normalized="https://ind.nl/en/residence-permits/work/highly-skilled-migrant",
        content_kind="html_page",
        language="en",
        title="IND Highly Skilled Migrant Synthetic Guidance",
        discovery_status="seed",
    )
    db_session.add(doc)
    db_session.flush()

    v1_text = "In 2022 the historical synthetic Dutch salary threshold was € 2.900 per month."
    v2_text = "Current synthetic Dutch salary threshold is €3,100 per month for under 30."

    v1 = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=src_fetcher.id,
        version_number=1,
        source_url=doc.canonical_url,
        content_type="text/html",
        language="nl",
        retrieved_at=datetime(2022, 5, 1, tzinfo=timezone.utc),
        source_published_at=datetime(2022, 1, 1, tzinfo=timezone.utc),
        legal_status="historical",
        http_status=200,
        content_sha256=_sha("v1-raw"),
        byte_size=len(v1_text),
        storage_path="raw/documents/1/v1.html",
        is_current=False,
    )
    v2 = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=src_primary.id,
        version_number=2,
        source_url=doc.canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        source_published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        legal_status="unknown",
        http_status=200,
        content_sha256=_sha("v2-raw"),
        byte_size=len(v2_text),
        storage_path="raw/documents/1/v2.html",
        is_current=True,
    )
    db_session.add_all([v1, v2])
    db_session.flush()

    for ver, txt, idx in ((v1, v1_text, 0), (v2, v2_text, 0)):
        c = DocumentChunk(
            document_id=doc.id,
            document_version_id=ver.id,
            source_id=src_primary.id,
            jurisdiction_id=jur_nl.id,
            jurisdiction_code="NL",
            chunk_index=idx,
            char_start=12,
            char_end=12 + len(txt),
            heading_path="IND Highly Skilled Migrant > Salary",
            content_text=txt,
            content_sha256=_sha(txt),
            is_current_version=bool(ver.is_current),
        )
        db_session.add(c)
        db_session.flush()
        vec = emb.embed_passages([txt]).vectors[0]
        db_session.add(
            DocumentChunkEmbedding(
                chunk_id=c.id,
                embedding_model_id=PLANNED_EMBEDDING_MODEL_ID,
                embedding=list(map(float, vec.tolist())),
            )
        )
    db_session.flush()

    retriever = InternalKnowledgeRetriever(db_session, embedder=emb)
    cur_res = retriever.retrieve(InternalQuery(query="salary threshold", jurisdiction="NL", mode="hybrid", top_k=5))
    assert len(cur_res.passages) == 1
    p_cur = cur_res.passages[0]
    assert p_cur.source_id == src_primary.id
    assert p_cur.source_name == "IND Official Portal"
    assert p_cur.authority_id == auth_ind.id
    assert p_cur.authority_name == "Immigration and Naturalisation Service (IND)"
    assert p_cur.fetched_via_source_id == src_primary.id
    assert p_cur.version_number == 2
    assert p_cur.chunk_index == 0
    assert p_cur.char_start == 12
    assert p_cur.char_end == 12 + len(v2_text)
    assert p_cur.chunk_content_sha256 == _sha(v2_text)
    assert p_cur.version_content_sha256 == _sha("v2-raw")
    assert p_cur.language == "en"

    # Orchestration maps complete lineage onto EvidenceItem without promoting verification
    @contextmanager
    def _bound():
        yield db_session

    orch = EvidenceOrchestrator(session_factory=_bound, embedder=emb)
    res_cur = orch.gather(question="salary threshold", jurisdiction="NL", external_mode="off")
    ev_cur = res_cur.evidence[0]
    assert ev_cur.verification_state == EvidenceVerificationState.UNVERIFIED_RETRIEVAL
    assert ev_cur.source_id == src_primary.id
    assert ev_cur.authority_id == auth_ind.id
    assert ev_cur.authority_name == "Immigration and Naturalisation Service (IND)"
    assert ev_cur.source_name == "IND Official Portal"
    assert ev_cur.document_version_number == 2
    assert ev_cur.char_start == 12 and ev_cur.char_end == 12 + len(v2_text)
    assert ev_cur.content_sha256 == _sha(v2_text)
    assert ev_cur.provenance["version_content_sha256"] == _sha("v2-raw")
    assert ev_cur.language == "en"
    assert ev_cur.locale_hint is None  # plain 'en' does not guess Anglo vs EU locale
    assert ev_cur.source_authority_category == SourceAuthorityCategory.OFFICIAL_GUIDANCE.value

    # Historical query retrieves v1 with distinct version_number=1, fetched_via_source_id, language="nl" -> locale_hint="eu"
    res_hist = orch.gather(
        question="salary threshold 2022",
        jurisdiction="NL",
        as_of_date=date(2022, 6, 1),
        external_mode="off",
    )
    ev_hist = next(e for e in res_hist.evidence if e.document_version_id == v1.id)
    assert ev_hist.document_version_number == 1
    assert ev_hist.fetched_via_source_id == src_fetcher.id
    assert ev_hist.language == "nl"
    assert ev_hist.locale_hint == "eu"
    assert ev_hist.legal_status == "historical"


# ---------------------------------------------------------------------------
# 4 & 13: Controlled Source Authority Taxonomy & Unknown Preservation
# ---------------------------------------------------------------------------


def test_source_authority_taxonomy_and_unknown_preservation():
    # Primary legislation
    cat_leg, auth_leg = classify_source_authority(
        source_type="external_api",
        source_url="https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32021L1883",
        provider="eurlex_cellar",
        external_source_type="legislation",
    )
    assert cat_leg == SourceAuthorityCategory.PRIMARY_LEGISLATION
    assert "EUR-Lex" in (auth_leg or "")

    # Binding rules (US final rule & Curated rule)
    cat_fr, auth_fr = classify_source_authority(
        source_type="external_api",
        source_url="https://www.federalregister.gov/documents/2026-001",
        provider="us_federal_register",
        external_source_type="final_rule",
        metadata={"agencies": ["USCIS"]},
    )
    assert cat_fr == SourceAuthorityCategory.OFFICIAL_BINDING_RULES
    assert auth_fr == "USCIS"

    # Official guidance vs portal hub
    cat_gui, _ = classify_source_authority(
        source_type="external_api",
        source_url="https://www.gov.uk/skilled-worker-visa",
        provider="govuk_content",
        external_source_type="guidance",
    )
    assert cat_gui == SourceAuthorityCategory.OFFICIAL_GUIDANCE

    cat_portal, _ = classify_source_authority(
        source_type="external_api",
        source_url="https://www.uscis.gov/working-in-the-united-states",
        provider="national_official",
        external_source_type="guidance",
    )
    assert cat_portal == SourceAuthorityCategory.GOVERNMENT_PORTAL

    # Statistical dataset
    cat_ds, _ = classify_source_authority(
        source_type="external_api",
        source_url="https://open.canada.ca/data/en/dataset/immigration-stats",
        provider="canada_open",
        external_source_type="dataset",
        legal_status="dataset_catalog",
    )
    assert cat_ds == SourceAuthorityCategory.GOVERNMENT_STATISTICAL_DATASET

    # Unknown internal authority must remain UNKNOWN (never fabricated)
    ev_unknown = evidence_from_internal_passage(
        jurisdiction="GB",
        official_url="https://example.test/synthetic-unknown-source",
        document_id=1,
        document_version_id=1,
        chunk_id=1,
        content_text="Synthetic guidance without authority record.",
    )
    assert ev_unknown.source_authority_category == SourceAuthorityCategory.UNKNOWN.value
    assert ev_unknown.issuing_authority is None
    assert ev_unknown.legal_status == "unknown"
    assert ev_unknown.language is None
    assert ev_unknown.locale_hint is None
    assert ev_unknown.verification_state == EvidenceVerificationState.UNVERIFIED_RETRIEVAL


# ---------------------------------------------------------------------------
# 5–7: Statistical vs Normative, Proposed/Withdrawn, EU vs National
# ---------------------------------------------------------------------------


def test_statistical_dataset_cannot_verify_normative_or_corroborate():
    ds = evidence_from_external_item(
        jurisdiction="CA",
        official_url="https://open.canada.ca/data/en/dataset/express-entry-stats",
        provider="canada_open",
        passage_text=(
            "Express Entry Admissions Dataset. Statistical summary notes 490 "
            "points median CRS score in 2025. Open Government datasets are not "
            "IRCC visa eligibility rules."
        ),
        source_type_label="dataset",
        legal_status="dataset_catalog",
        metadata={"organization": "IRCC"},
    )
    assert ds.source_authority_category == SourceAuthorityCategory.GOVERNMENT_STATISTICAL_DATASET.value
    assert ds.issuing_authority == "IRCC"

    c_norm = AnswerClaim(
        claim="The Express Entry minimum required CRS score is 490 points.",
        evidence_ids=[ds.evidence_id],
    )
    res = verify_material_claims(
        answer=c_norm.claim,
        claims=[c_norm],
        evidence=[ds],
        jurisdiction="CA",
        pathway_id="ca_express_entry",
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
    assert "statistical_dataset_cannot_verify_normative_requirement" in res.outcomes[0].reasons
    assert res.outcomes[0].retained is False
    assert "490" not in (res.answer or "")

    # Statistical dataset also cannot serve as independent distinct corroboration
    norm_page = evidence_from_internal_passage(
        jurisdiction="CA",
        official_url="https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry.html",
        document_id=50,
        document_version_id=50,
        chunk_id=1,
        content_text="Express Entry minimum CRS score example is 490 points under this round.",
    )
    c_real = AnswerClaim(
        claim="Express Entry CRS score example is 490 points.",
        evidence_ids=[norm_page.evidence_id],
    )
    res_corr = verify_material_claims(
        answer=c_real.claim,
        claims=[c_real],
        evidence=[norm_page, ds],
        jurisdiction="CA",
        pathway_id="ca_express_entry",
        question="What is the minimum Express Entry threshold score?",
    )
    assert res_corr.outcomes[0].independent_corroboration.value != "reused_retrieval_distinct_source"


def test_proposed_withdrawn_and_eu_directive_safeguards():
    # Proposed EU CELEX 5... and US Proposed Rule excluded from current evidence
    eu_prop = evidence_from_external_item(
        jurisdiction="EU",
        official_url="https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52026PC0001",
        provider="eurlex_cellar",
        passage_text="Proposed EU synthetic single permit minimum fee €999.",
        source_type_label="proposed_rule",
        legal_status="proposed_or_preparatory",
    )
    gov_withdrawn = evidence_from_external_item(
        jurisdiction="GB",
        official_url="https://www.gov.uk/withdrawn-guidance",
        provider="govuk_content",
        passage_text="Withdrawn synthetic route fee was £555.",
        source_type_label="guidance",
        legal_status="withdrawn",
    )
    sel = select_temporal_evidence(
        [eu_prop, gov_withdrawn],
        resolved_reference_date=date(2026, 11, 18),
        temporal_mode=TemporalQueryMode.CURRENT,
    )
    assert sel.kept_evidence == []
    assert len(sel.excluded_items) == 2

    # EU-level source stamped with national jurisdiction (e.g. eu_opendata NL) cannot verify NL rules
    eu_nl_stat = evidence_from_external_item(
        jurisdiction="NL",
        official_url="https://data.europa.eu/data/datasets/nl-migr",
        provider="eu_opendata",
        passage_text="Netherlands EU migration dataset mentions €38,000 minimum threshold.",
        source_type_label="dataset",
        legal_status="dataset_catalog",
        metadata={"eu_level_source": True},
    )
    c_nl = AnswerClaim(
        claim="The Dutch minimum salary threshold is €38,000.",
        evidence_ids=[eu_nl_stat.evidence_id],
    )
    res_nl = verify_material_claims(
        answer=c_nl.claim,
        claims=[c_nl],
        evidence=[eu_nl_stat],
        jurisdiction="NL",
        pathway_id="nl_highly_skilled_migrant",
    )
    assert res_nl.outcomes[0].retained is False
    assert "38,000" not in (res_nl.answer or "")

    # EU directive (eurlex) cannot verify national implementation on DE/NL query
    eu_dir = evidence_from_external_item(
        jurisdiction="DE",  # simulate directive passed into DE context
        official_url="https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32021L1883",
        provider="eurlex_cellar",
        passage_text="Directive (EU) 2021/1883 sets Blue Card salary framework at 1.0 times average gross salary.",
        source_type_label="legislation",
        legal_status="directive_may_need_national_implementation",
        metadata={"eu_level_source": True},
    )
    c_de = AnswerClaim(
        claim="Germany requires a minimum Blue Card salary multiplier under Directive 2021/1883.",
        evidence_ids=[eu_dir.evidence_id],
    )
    res_de = verify_material_claims(
        answer=c_de.claim,
        claims=[c_de],
        evidence=[eu_dir],
        jurisdiction="DE",
        pathway_id="de_eu_blue_card",
    )
    assert res_de.outcomes[0].status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
    assert "eu_level_source_cannot_verify_national_implementation" in res_de.outcomes[0].reasons
    assert res_de.outcomes[0].retained is False


# ---------------------------------------------------------------------------
# 8 & 16: External Connector Metadata & Legacy Extraction Fixes
# ---------------------------------------------------------------------------


def test_external_connectors_metadata_and_legacy_fixes():
    # Legacy fix: dig_body_html collects BOTH body AND need_to_know (previously dropped need_to_know)
    combined_html = dig_body_html(
        {
            "introduction": "<p>Introductory overview of Skilled Worker visa.</p>",
            "body": "<p>General salary threshold is £41,700 per year.</p>",
            "need_to_know": "<p>Exceptions apply to pre-4 April 2024 CoS holders.</p>",
        }
    )
    assert combined_html is not None
    assert "Introductory overview" in combined_html
    assert "£41,700" in combined_html
    assert "pre-4 April 2024" in combined_html

    # GOV.UK connector: organisations, withdrawn_notice, truncation
    def gov_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "base_path": "/skilled-worker-visa",
                "content_id": "gov-1",
                "title": "Skilled Worker visa",
                "locale": "en",
                "first_published_at": "2020-01-01T00:00:00Z",
                "public_updated_at": "2026-07-01T00:00:00Z",
                "links": {"organisations": [{"title": "Home Office"}, {"title": "UK Visas and Immigration"}]},
                "details": {
                    "body": "<p>You need a job offer from an approved sponsor.</p>",
                    "need_to_know": "<p>Minimum threshold conditions depend on occupation code.</p>",
                },
            },
        )

    http_g = _mock_http(gov_handler)
    gov_out = GovukContentConnector(http_g).search(ExternalQuery(pathway_id="gb_skilled_worker", jurisdiction="GB"))
    assert gov_out.items[0].metadata["issuing_authority"] == "Home Office, UK Visas and Immigration"
    assert any("Minimum threshold conditions" in p for p in gov_out.items[0].passages)

    # NationalOfficial connector: HTML lang, issuing_authority, table structure, truncation flag
    long_body = " ".join(["Important qualifying condition and statutory exception clause."] * 20)

    def nat_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            text=(
                '<html lang="nl"><body><h1>Kennismigrant</h1>'
                "<table><tr><th>Categorie</th><th>Bedrag</th></tr>"
                "<tr><td>30 jaar of ouder</td><td>€ 5.331,00</td></tr></table>"
                f"<p>{long_body}</p></body></html>"
            ),
            headers={"Last-Modified": "Wed, 01 Jul 2026 00:00:00 GMT"},
        )

    http_n = _mock_http(nat_handler)
    nat_out = NationalOfficialConnector(http_n).search(
        ExternalQuery(query="highly skilled migrant salary", jurisdiction="NL", pathway_id="nl_highly_skilled_migrant")
    )
    nat_item = nat_out.items[0]
    assert nat_item.language == "nl"
    assert nat_item.metadata["has_truncated_passages"] is True
    assert "IND" in (nat_item.metadata["issuing_authority"] or "")
    assert any("Categorie | Bedrag" in p and "€ 5.331,00" in p for p in nat_item.passages)

    # EUR-Lex: CELEX 5... mapped to SourceType.PROPOSED_RULE
    def eur_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "sparql" in url:
            return httpx.Response(
                200,
                request=request,
                json={"results": {"bindings": [{"title": {"value": "Proposal COM(2026) 1"}, "date": {"value": "2026-03-01"}}]}},
            )
        return httpx.Response(
            200,
            request=request,
            text="<html><body><p>Proposal for a Directive on third-country permit rules.</p></body></html>",
        )

    http_e = _mock_http(eur_handler)
    eur_out = EurlexCellarConnector(http_e).search(ExternalQuery(query="52026PC0001", jurisdiction="EU"))
    assert eur_out.items[0].source_type == SourceType.PROPOSED_RULE
    assert eur_out.items[0].legal_status == "proposed_or_preparatory"
    assert eur_out.items[0].metadata["eu_level_source"] is True

    # Canada Open: French-only metadata detects language="fr" (not hardcoded "en")
    def ca_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "success": True,
                "result": {
                    "results": [
                        {
                            "id": "ca-fr-1",
                            "name": "stats-fr",
                            "title": {"fr": "Statistiques sur l'immigration"},
                            "notes": {"fr": "Données annuelles d'admission des résidents permanents."},
                            "organization": {"title": "IRCC"},
                        }
                    ]
                },
            },
        )

    http_c = _mock_http(ca_handler)
    ca_out = CanadaOpenConnector(http_c).search(ExternalQuery(query="immigration", jurisdiction="CA"))
    assert ca_out.items[0].language == "fr"
    assert ca_out.items[0].metadata["issuing_authority"] == "IRCC"

    # US Federal Register: ISO effective_on in effective_at; free-text dates kept in metadata
    def fr_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {
                        "title": "H-1B Final Rule",
                        "type": "Rule",
                        "abstract": "DHS updates H-1B registration rules.",
                        "document_number": "2025-1234",
                        "html_url": "https://www.federalregister.gov/documents/2025-1234",
                        "publication_date": "2025-01-10",
                        "effective_on": None,
                        "dates": "Effective 60 days after publication; comments due later.",
                        "agencies": [{"name": "DHS"}],
                    }
                ]
            },
        )

    http_f = _mock_http(fr_handler)
    fr_out = UsFederalRegisterConnector(http_f).search(ExternalQuery(query="H-1B", jurisdiction="US"))
    assert fr_out.items[0].effective_at is None
    assert "Effective 60 days" in fr_out.items[0].metadata["dates_text"]
    assert fr_out.items[0].language == "en-US"

    # EU Immigration Portal stub classified as GOVERNMENT_PORTAL
    def eip_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, text="<html><body>Portal</body></html>")

    http_p = _mock_http(eip_handler)
    eip_out = EuImmigrationPortalConnector(http_p).search(ExternalQuery(jurisdiction="EU"))
    ev_eip = evidence_from_external_item(
        jurisdiction="EU",
        official_url=eip_out.items[0].official_url,
        provider="eu_immigration_portal",
        passage_text="EU Immigration Portal landing information.",
        legal_status=eip_out.items[0].legal_status,
        metadata=eip_out.items[0].metadata,
    )
    assert ev_eip.source_authority_category == SourceAuthorityCategory.GOVERNMENT_PORTAL.value
    assert ev_eip.issuing_authority == "European Commission"


# ---------------------------------------------------------------------------
# 9: Passage Integrity, Table Headers, and Truncated-Excerpt Guard
# ---------------------------------------------------------------------------


def test_table_chunking_preserves_headers_and_whitespace_boundaries():
    rows = [[f"Occupation {i:03d} Specialist Role", f"£{30000 + i * 100:,}"] for i in range(40)]
    payload = {
        "title": "Going Rates Table",
        "blocks": [
            {"type": "heading", "level": 1, "text": "Occupation Going Rates"},
            {
                "type": "table",
                "caption": "Synthetic Going Rates",
                "headers": ["Occupation", "Annual Minimum"],
                "rows": rows,
            },
        ],
    }
    drafts = chunk_structured_document(payload, min_chars=40)
    assert len(drafts) > 1
    assert all(len(d.content_text) <= E5_SAFE_MAX_CHARS for d in drafts)
    # Continuation table chunks preserve column labels!
    assert all("Occupation | Annual Minimum" in d.content_text for d in drafts)


def test_truncation_marked_in_packing_and_blocks_unconditional_exclusivity():
    passages, truncated = extract_passages_with_truncation(
        "The general salary threshold is £41,700 per year. " + ("Detailed exception conditions apply. " * 25),
        max_passages=1,
        max_chars=120,
    )
    assert truncated is True
    assert "41,700" in passages[0]

    # Single long paragraph sliced at max_chars=85 marks …[truncated] on whitespace boundary
    long_para = (
        "The general salary threshold is £41,700 per year unless a qualifying "
        "transitional exception or occupation discount applies."
    )
    p2, trunc2 = extract_passages_with_truncation(long_para, max_passages=1, max_chars=85)
    assert trunc2 is True
    assert "…[truncated]" in p2[0]
    assert "41,700" in p2[0]

    ev = evidence_from_internal_passage(
        jurisdiction="GB",
        official_url="https://www.gov.uk/skilled-worker-visa",
        document_id=200,
        document_version_id=201,
        chunk_id=1,
        content_text="The general salary threshold is £41,700 per year. " + ("Additional conditions apply. " * 20),
    )
    packed = pack_evidence_context(
        [ev],
        max_items=5,
        max_chars_per_passage=100,
        max_total_chars=300,
    )
    assert packed[0]["is_truncated"] is True
    assert "excerpt truncated" in (packed[0]["temporal_note"] or "").lower()
    prompt = build_user_prompt(
        question="What is the salary?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        user_circumstances=None,
        evidence_payload=packed,
    )
    assert "excerpt_truncated: true" in prompt
    assert "authority_category: official_guidance" in prompt

    # Unconditional exclusivity claim over truncated excerpt must be qualified (TEMPORAL_UNCERTAINTY)
    ev_trunc = evidence_from_internal_passage(
        jurisdiction="GB",
        official_url="https://www.gov.uk/skilled-worker-visa",
        document_id=202,
        document_version_id=203,
        chunk_id=1,
        content_text="The general salary threshold is £41,700 per year …[truncated]",
        is_truncated=True,
    )
    c_excl = AnswerClaim(
        claim="The Skilled Worker general salary threshold is only £41,700 per year for all applicants.",
        evidence_ids=[ev_trunc.evidence_id],
    )
    res = verify_material_claims(
        answer=c_excl.claim,
        claims=[c_excl],
        evidence=[ev_trunc],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY
    assert "cited_excerpt_truncated_qualifiers_may_be_omitted" in res.outcomes[0].reasons
    assert "truncated" in (res.answer or "").lower()


# ---------------------------------------------------------------------------
# 10–11 & 15: HTML/Document Locale Propagation -> 10C.1 Numeric Safety
# ---------------------------------------------------------------------------


def test_html_language_extraction_keeps_content_hash_invariant_and_drives_locale():
    html_plain = "<html><body><h1>Fee</h1><p>Der Mindestbetrag beträgt €38.000.</p></body></html>"
    html_de = '<html lang="de-DE"><body><h1>Fee</h1><p>Der Mindestbetrag beträgt €38.000.</p></body></html>'

    doc_plain = extract_html_document(html_plain)
    doc_de = extract_html_document(html_de)
    assert doc_plain.language is None
    assert doc_de.language == "de-DE"
    # Critical invariant: language metadata stays outside canonical content hash!
    assert normalized_content_sha256(doc_plain) == normalized_content_sha256(doc_de)

    # Unknown / plain 'en' / jurisdiction='NL' with language=None NEVER guesses 'eu'
    assert locale_hint_from_language(None) is None
    assert locale_hint_from_language("en") is None
    assert locale_hint_from_language("de-DE") == "eu"
    assert locale_hint_from_language("nl") == "eu"
    assert locale_hint_from_language("en-GB") == "gb"
    assert normalize_language_tag("und") is None

    # Unknown language: €38.000 fails safely (AMBIGUOUS -> INSUFFICIENT_EVIDENCE)
    ev_no_lang = evidence_from_internal_passage(
        jurisdiction="NL",
        official_url="https://ind.nl/en/test",
        document_id=301,
        document_version_id=301,
        chunk_id=1,
        content_text="Minimum synthetic salary threshold is €38.000 per year.",
        language=None,
    )
    assert evidence_locale_hint(ev_no_lang) is None
    c_guess = AnswerClaim(
        claim="Minimum synthetic salary threshold is €38,000 per year.",
        evidence_ids=[ev_no_lang.evidence_id],
    )
    res_unk = verify_material_claims(
        answer=c_guess.claim, claims=[c_guess], evidence=[ev_no_lang], jurisdiction="NL", pathway_id=None
    )
    assert res_unk.outcomes[0].retained is False
    assert "38,000" not in (res_unk.answer or "")

    # Explicit language="nl": €38.000 resolves deterministically as 38000!
    ev_nl_lang = evidence_from_internal_passage(
        jurisdiction="NL",
        official_url="https://ind.nl/nl/test",
        document_id=302,
        document_version_id=302,
        chunk_id=1,
        content_text="Minimum synthetic salary threshold is €38.000 per year.",
        language="nl",
    )
    assert evidence_locale_hint(ev_nl_lang) == "eu"
    c_nl_ok = AnswerClaim(
        claim="Minimum synthetic salary threshold is €38,000 per year.",
        evidence_ids=[ev_nl_lang.evidence_id],
    )
    res_nl_ok = verify_material_claims(
        answer=c_nl_ok.claim, claims=[c_nl_ok], evidence=[ev_nl_lang], jurisdiction="NL", pathway_id=None
    )
    assert res_nl_ok.outcomes[0].status == ClaimVerificationStatus.SUPPORTED


# ---------------------------------------------------------------------------
# 12: Consolidated Pathway Attribution & Cross-Pathway Hard Exclusions
# ---------------------------------------------------------------------------


def test_consolidated_pathway_attribution_and_hard_exclusions():
    # Consolidated URL tokens from coverage + pathway_seeds.json
    assert "au_temporary_graduate_485" in pathway_ids_from_url(
        "https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/temporary-graduate-485"
    )
    assert "us_h1b" in pathway_ids_from_url(
        "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations"
    )
    assert "nl_fees" in pathway_ids_from_url(
        "https://ind.nl/en/fees-costs-of-an-application"
    )

    # Hard exclusion: AU Student 500 cannot support Temporary Graduate 485
    ev_500 = evidence_from_internal_passage(
        jurisdiction="AU",
        official_url="https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500",
        document_id=401,
        document_version_id=401,
        chunk_id=1,
        content_text="Student visa (subclass 500) financial capacity requirement is AUD 29,710 per year.",
    )
    pa = classify_pathway_applicability(
        claim="The Temporary Graduate 485 requirement is AUD 29,710 per year.",
        cited=[ev_500],
        pathway_id="au_temporary_graduate_485",
    )
    assert pa.classification == PathwayApplicability.CROSS_PATHWAY

    # Hard exclusion: US international-entrepreneur rule cannot support us_h1b
    ev_ie_rule = evidence_from_internal_passage(
        jurisdiction="US",
        official_url="https://www.uscis.gov/working-in-the-united-states/international-entrepreneur-rule",
        document_id=402,
        document_version_id=402,
        chunk_id=1,
        content_text="International Entrepreneur Rule investment amount is USD 264,147.",
    )
    pa_us = classify_pathway_applicability(
        claim="H-1B specialty occupation requires USD 264,147.",
        cited=[ev_ie_rule],
        pathway_id="us_h1b",
    )
    assert pa_us.classification == PathwayApplicability.CROSS_PATHWAY

    # UK Student claim must NOT collide with jp_student / nz_student_visa
    ev_uk_stu = evidence_from_internal_passage(
        jurisdiction="GB",
        official_url="https://www.gov.uk/student-visa",
        document_id=403,
        document_version_id=403,
        chunk_id=1,
        content_text="Student visa permission limits term-time work to 20 hours per week.",
    )
    pa_uk = classify_pathway_applicability(
        claim="Student visa permission limits term-time work to 20 hours per week.",
        cited=[ev_uk_stu],
        pathway_id="gb_student",
    )
    assert pa_uk.classification == PathwayApplicability.DIRECT


# ---------------------------------------------------------------------------
# 17–19: API Compatibility, Zero Extra LLM Calls & No Unauthorized Promotion
# ---------------------------------------------------------------------------


def test_api_compatibility_and_zero_extra_llm_calls(reset_agent_runtime, monkeypatch):
    import lifepilot.api.app as api_app

    ev_ind = evidence_from_internal_passage(
        jurisdiction="NL",
        official_url="https://ind.nl/en/fees-costs-of-an-application",
        document_id=501,
        document_version_id=501,
        chunk_id=1,
        source_id=11041,
        source_name="IND Fees",
        authority_id=77,
        authority_name="IND",
        document_version_number=3,
        chunk_index=0,
        char_start=0,
        char_end=90,
        content_sha256=_sha("ind-fees"),
        language="en",
        content_text="IND Fees: cross-border permit fee is € 85,00 per application.",
    )

    class _MockOrch:
        def gather(self, **_kwargs):
            return OrchestrationResult(
                evidence=[ev_ind],
                status=AgentStatus.SUCCESS,
                internal_status="ok",
                resolved_reference_date="2026-11-18",
                temporal_mode="current",
                temporal_outcome="current_candidate_unverified",
            )

    set_evidence_orchestrator_override(_MockOrch())
    llm = _CountingFakeLLM(
        lambda _p: GroundedAnswer(
            answer="IND cross-border permit fee is €85.00 per application.",
            claims=[
                AnswerClaim(
                    claim="IND Fees state the cross-border permit fee is €85.00 per application.",
                    evidence_ids=[ev_ind.evidence_id],
                )
            ],
        )
    )
    set_llm_provider_override(llm)

    monkeypatch.setenv("LIFEPILOT_ENV", "development")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_AUTH", "1")
    monkeypatch.setenv("LIFEPILOT_API_KEYS", "c2-key")
    monkeypatch.setenv("LIFEPILOT_VERIFICATION_ENABLED", "1")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_LLM", "0")
    reset_api_settings_cache()
    reset_agent_config_cache()
    reset_rate_limit_state()

    client = TestClient(api_app.create_app())
    r = client.post(
        "/v1/answer",
        headers={"X-API-Key": "c2-key"},
        json={
            "question": "What is the IND cross-border permit fee?",
            "jurisdiction": "NL",
            "pathway_id": "nl_fees_hub",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert llm.calls == 1
    assert body["status"] == "success"
    assert "85.00" in (body["answer"] or "")
    assert body["evidence_count"] == 1

    # Full run_agent preserves full EvidenceItem lineage without unauthorized verification promotion
    resp = run_agent(
        AgentRequest(
            question="What is the IND cross-border permit fee?",
            jurisdiction="NL",
            pathway_id="nl_fees_hub",
        ),
        config=AgentConfig(llm_provider="fake", llm_model="fake", verification_enabled=True),
    )
    assert resp.evidence[0].verification_state == EvidenceVerificationState.UNVERIFIED_RETRIEVAL
    assert resp.evidence[0].authority_name == "IND"
    assert resp.evidence[0].document_version_number == 3
    assert resp.evidence[0].source_authority_category == SourceAuthorityCategory.OFFICIAL_GUIDANCE.value
