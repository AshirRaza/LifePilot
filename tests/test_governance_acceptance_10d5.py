"""Phase 10D.5 — Integrated governance acceptance, forgery gate, strict-mode E2E.

Synthetic fixtures only. No live DB mutations. No fabricated real legal approvals.
"""

from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import Session

from lifepilot.agent.contracts import AnswerClaim
from lifepilot.agent.evidence import (
    EvidenceItem,
    EvidenceSourceType,
    EvidenceVerificationState,
    evidence_from_legal_rule,
)
from lifepilot.agent.orchestration import EvidenceOrchestrator
from lifepilot.agent.verification import ClaimVerificationStatus, verify_material_claims
from lifepilot.db.legal_review import (
    ActorRef,
    append_review_event,
    create_pending_review_item,
    get_review_item,
)
from lifepilot.db.models import (
    Authority,
    Document,
    DocumentVersion,
    Jurisdiction,
    LegalRule,
    Source,
)
from lifepilot.governance.approval_invariant import validate_legal_rule_human_authority
from lifepilot.governance.high_risk_queue_import import import_high_risk_queue
from lifepilot.governance.ingest_version_guard import enforce_review_pins_on_new_current
from lifepilot.governance.legal_rule_promotion import promote_legal_rule_from_approval
from lifepilot.governance.maintenance_alerts import build_freshness_alerts
from lifepilot.governance.reassessment import build_reassessment_queue
from lifepilot.governance.review_workflow import ReviewWorkflowService
from lifepilot.governance.reviewer_auth import (
    PERM_LEGAL_REVIEW_ADMIN,
    PERM_LEGAL_REVIEW_ASSIGN,
    PERM_LEGAL_REVIEW_DECIDE,
    PERM_LEGAL_REVIEW_READ,
    ReviewerIdentity,
)
from lifepilot.governance.source_change import SourceChangeMateriality
from lifepilot.governance.verification_policy import (
    LegalVerificationPolicy,
    reset_legal_verification_policy_cache,
    resolve_effective_policy,
    strict_mode_blocks_high_risk_claim,
)
from lifepilot.rag.legal_rules import query_reviewed_legal_rules


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _seed(session: Session, *, code: str) -> dict:
    jur = Jurisdiction(
        code=code,
        name=f"Synthetic {code}",
        jurisdiction_type="country",
        country_code="XX",
    )
    session.add(jur)
    session.flush()
    auth = Authority(jurisdiction_id=jur.id, name=f"Auth {code}")
    session.add(auth)
    session.flush()
    source = Source(
        authority_id=auth.id,
        jurisdiction_id=jur.id,
        name=f"Source {code}",
        seed_url=f"https://example.test/{code.lower()}",
        seed_url_normalized=f"https://example.test/{code.lower()}",
        allowed_url_patterns=[f"https://example.test/{code.lower()}/*"],
        review_status="approved",
        default_language="en",
    )
    session.add(source)
    session.flush()
    doc = Document(
        authority_id=auth.id,
        primary_source_id=source.id,
        canonical_url=f"https://example.test/{code.lower()}/rules",
        url_normalized=f"https://example.test/{code.lower()}/rules",
        content_kind="html_page",
        language="en",
        discovery_status="seed",
    )
    session.add(doc)
    session.flush()
    v1 = DocumentVersion(
        document_id=doc.id,
        fetched_via_source_id=source.id,
        version_number=1,
        source_url=doc.canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
        http_status=200,
        content_sha256=_sha(f"{code}-v1"),
        byte_size=120,
        storage_path=f"data/raw/test/{code.lower()}_v1.html",
        is_current=True,
        legal_status="unknown",
    )
    session.add(v1)
    session.flush()
    rule = LegalRule(
        rule_key=f"{code.lower()}.salary.general",
        rule_revision=1,
        jurisdiction_id=jur.id,
        jurisdiction_code=jur.code,
        pathway_id="xx_route",
        requirement_category="financial_or_sponsorship",
        requirement_type="salary_threshold",
        numeric_value=Decimal("41000.00"),
        currency_code="GBP",
        amount_unit="yearly",
        effective_from=date(2024, 1, 1),
        effective_to=None,
        effective_from_inclusive=True,
        effective_to_inclusive=True,
        effective_date_status="known_open_ended",
        cohort_key="general",
        cohort_category="general",
        is_transitional=False,
        rule_summary="Synthetic 10D.5 governance acceptance rule.",
        source_id=source.id,
        document_id=doc.id,
        document_version_id=v1.id,
        source_url=doc.canonical_url,
    )
    session.add(rule)
    session.flush()
    return {
        "jurisdiction": jur,
        "source": source,
        "document": doc,
        "v1": v1,
        "rule": rule,
    }


def _human(actor_id: str = "reviewer:alice") -> ReviewerIdentity:
    return ReviewerIdentity(
        actor_id=actor_id,
        actor_kind="human_reviewer",
        permissions=frozenset(
            {
                PERM_LEGAL_REVIEW_READ,
                PERM_LEGAL_REVIEW_ASSIGN,
                PERM_LEGAL_REVIEW_DECIDE,
                PERM_LEGAL_REVIEW_ADMIN,
            }
        ),
    )


def _mech_evidence() -> EvidenceItem:
    return EvidenceItem(
        evidence_id="ev_mech_10d5",
        source_type=EvidenceSourceType.INTERNAL_RAG,
        verification_state=EvidenceVerificationState.UNVERIFIED_RETRIEVAL,
        jurisdiction="GB",
        source_url="https://www.gov.uk/skilled-worker-visa",
        passage_text="Skilled Worker salary threshold is GBP 41,700 a year.",
    )


# ---------------------------------------------------------------------------
# End-to-end lifecycle
# ---------------------------------------------------------------------------


def test_e2e_unreviewed_to_approved_promotion_and_strict_accept(db_session: Session):
    g = _seed(db_session, code="XX-E2E1")
    assert g["rule"].verification_status == "unverified"

    svc = ReviewWorkflowService(db_session)
    alice = _human()
    item = create_pending_review_item(
        db_session,
        subject_type="legal_rule",
        jurisdiction_id=g["jurisdiction"].id,
        legal_rule_id=g["rule"].id,
        document_version_id=g["v1"].id,
        document_id=g["document"].id,
        source_id=g["source"].id,
        pathway_id="xx_route",
        review_reason="E2E pending review.",
        actor=ActorRef(actor_id="service:seed", actor_kind="service"),
        reviewed_cohort_key="general",
        reviewed_effective_from=date(2024, 1, 1),
        risk_class="high",
    )
    svc.assign(
        alice,
        review_item_id=item.id,
        expected_row_version=1,
        assignee_actor_id=alice.actor_id,
        rationale="Assign to self.",
    )
    db_session.refresh(item)
    svc.start_review(
        alice, review_item_id=item.id, expected_row_version=item.row_version
    )
    db_session.refresh(item)
    from lifepilot.governance.review_workflow import ReviewDecisionRequest

    svc.submit_decision(
        alice,
        review_item_id=item.id,
        request=ReviewDecisionRequest(
            decision="approved",
            expected_row_version=item.row_version,
            rationale="Synthetic isolated human approval for E2E.",
            document_version_id=g["v1"].id,
            acknowledge_effective_period=True,
            acknowledge_cohort_conditions=True,
        ),
    )
    db_session.refresh(item)
    assert item.workflow_state == "approved"

    # Approval alone must not promote LegalRule.
    assert g["rule"].verification_status == "unverified"
    promote_legal_rule_from_approval(
        db_session,
        review_item_id=item.id,
        identity=alice,
        expected_item_row_version=item.row_version,
        rationale="Explicit promotion after approval.",
    )
    db_session.refresh(g["rule"])
    assert g["rule"].verification_status == "human_verified"
    assert validate_legal_rule_human_authority(db_session, g["rule"]).ok

    curated = evidence_from_legal_rule(g["rule"], session=db_session)
    reset_legal_verification_policy_cache()
    blocked, _ = strict_mode_blocks_high_risk_claim(
        question="What is the salary threshold?",
        claim_text="The salary threshold is GBP 41,000.",
        evidence=[curated],
        policy=LegalVerificationPolicy.STRICT_VERIFIED_ONLY,
    )
    assert blocked is False


def test_strict_rejects_mechanical_and_unapproved(monkeypatch, db_session: Session):
    monkeypatch.setenv("LIFEPILOT_LEGAL_VERIFICATION_POLICY", "strict_verified_only")
    reset_legal_verification_policy_cache()
    mech = _mech_evidence()
    blocked, reason = strict_mode_blocks_high_risk_claim(
        question="What is the Skilled Worker salary threshold?",
        claim_text="The Skilled Worker salary threshold is GBP 41,700.",
        evidence=[mech],
    )
    assert blocked is True
    assert reason and "strict_verified_only" in reason

    claim = AnswerClaim(
        claim="The Skilled Worker salary threshold is GBP 41,700.",
        evidence_ids=[mech.evidence_id],
    )
    res = verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[mech],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        question="What is the Skilled Worker salary threshold?",
    )
    assert res.outcomes[0].status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
    assert any("strict_verified_only" in r for r in res.outcomes[0].reasons)
    reset_legal_verification_policy_cache()


def test_caller_cannot_downgrade_strict_policy(monkeypatch):
    monkeypatch.setenv("LIFEPILOT_LEGAL_VERIFICATION_POLICY", "strict_verified_only")
    reset_legal_verification_policy_cache()
    assert (
        resolve_effective_policy(requested="advisory", allow_client_override=True)
        == LegalVerificationPolicy.STRICT_VERIFIED_ONLY
    )
    # AnswerRequest forbids policy field (extra=forbid) — covered by schema.
    from lifepilot.api.schemas import AnswerRequest

    with pytest.raises(Exception):
        AnswerRequest(
            question="salary?",
            jurisdiction="GB",
            legal_verification_policy="advisory",  # type: ignore[call-arg]
        )
    reset_legal_verification_policy_cache()


def test_mismatched_pins_block_authority(db_session: Session):
    g = _seed(db_session, code="XX-E2E2")
    from lifepilot.governance.synthetic_authority import ensure_synthetic_human_authority

    ensure_synthetic_human_authority(db_session, g["rule"], actor_id="reviewer:alice")
    g["rule"].cohort_key = "other"
    assert validate_legal_rule_human_authority(db_session, g["rule"]).ok is False
    g["rule"].cohort_key = "general"
    g["rule"].rule_revision = 99
    assert "rule_revision_mismatch" in validate_legal_rule_human_authority(
        db_session, g["rule"]
    ).reasons
    g["rule"].rule_revision = 1
    v2 = DocumentVersion(
        document_id=g["document"].id,
        fetched_via_source_id=g["source"].id,
        version_number=2,
        source_url=g["document"].canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
        http_status=200,
        content_sha256=_sha("XX-E2E2-v2"),
        byte_size=140,
        storage_path="data/raw/test/xx-e2e2_v2.html",
        is_current=False,
        legal_status="unknown",
    )
    db_session.add(v2)
    db_session.flush()
    g["rule"].document_version_id = v2.id
    check = validate_legal_rule_human_authority(db_session, g["rule"])
    assert check.ok is False
    assert "document_version_pin_mismatch" in check.reasons


def test_source_change_invalidation_rejects_stale_and_requires_reapproval(
    db_session: Session,
):
    g = _seed(db_session, code="XX-E2E3")
    from lifepilot.governance.synthetic_authority import ensure_synthetic_human_authority

    ensure_synthetic_human_authority(db_session, g["rule"], actor_id="reviewer:alice")
    v2 = DocumentVersion(
        document_id=g["document"].id,
        fetched_via_source_id=g["source"].id,
        version_number=2,
        source_url=g["document"].canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime(2026, 11, 21, tzinfo=timezone.utc),
        http_status=200,
        content_sha256=_sha("XX-E2E3-v2"),
        byte_size=160,
        storage_path="data/raw/test/xx-e2e3_v2.html",
        is_current=True,
        legal_status="unknown",
    )
    g["v1"].is_current = False
    db_session.add(v2)
    db_session.flush()
    res = enforce_review_pins_on_new_current(
        db_session,
        previous_version_id=g["v1"].id,
        new_version_id=v2.id,
        document_id=g["document"].id,
        content_hashes_equal=False,
        previous_text="old threshold 41000",
        new_text="new potentially material salary threshold 99000",
    )
    assert res.get("invalidated", 0) >= 1
    db_session.refresh(g["rule"])
    assert g["rule"].verification_status != "human_verified"
    assert validate_legal_rule_human_authority(db_session, g["rule"]).ok is False
    lookup = query_reviewed_legal_rules(
        db_session,
        jurisdiction=g["jurisdiction"].code,
        pathway_id="xx_route",
        as_of_date=date(2026, 6, 1),
    )
    assert all(r.id != g["rule"].id for r in lookup.matched_rules)

    # Reapproval requires a new review on the new pin (not silent restore).
    g["rule"].document_version_id = v2.id
    db_session.flush()
    from lifepilot.governance.synthetic_authority import ensure_synthetic_human_authority

    ensure_synthetic_human_authority(db_session, g["rule"], actor_id="reviewer:alice")
    assert validate_legal_rule_human_authority(db_session, g["rule"]).ok


def test_reassessment_and_freshness_alerts(db_session: Session):
    g = _seed(db_session, code="XX-E2E4")
    item = create_pending_review_item(
        db_session,
        subject_type="legal_rule",
        jurisdiction_id=g["jurisdiction"].id,
        legal_rule_id=g["rule"].id,
        document_version_id=g["v1"].id,
        review_reason="Reassessment fixture.",
        actor=ActorRef(actor_id="service:seed", actor_kind="service"),
        risk_class="high",
        reassessment_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )
    queue = build_reassessment_queue(
        db_session, as_of=datetime(2026, 11, 22, tzinfo=timezone.utc)
    )
    assert any(e.review_item_id == item.id for e in queue.entries)
    alerts = build_freshness_alerts(reassessment_entries=queue.to_dict()["entries"])
    assert isinstance(alerts, dict)


def test_ingest_failure_injection_rolls_back_authority(db_session: Session):
    g = _seed(db_session, code="XX-E2E5")
    from lifepilot.governance.synthetic_authority import ensure_synthetic_human_authority

    ensure_synthetic_human_authority(db_session, g["rule"], actor_id="reviewer:alice")
    v2 = DocumentVersion(
        document_id=g["document"].id,
        fetched_via_source_id=g["source"].id,
        version_number=2,
        source_url=g["document"].canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
        http_status=200,
        content_sha256=_sha("XX-E2E5-v2"),
        byte_size=160,
        storage_path="data/raw/test/xx-e2e5_v2.html",
        is_current=True,
        legal_status="unknown",
    )
    g["v1"].is_current = False
    db_session.add(v2)
    db_session.flush()
    with pytest.raises(RuntimeError):
        with db_session.begin_nested():
            with patch(
                "lifepilot.governance.ingest_version_guard.process_source_version_change",
                side_effect=RuntimeError("injected"),
            ):
                enforce_review_pins_on_new_current(
                    db_session,
                    previous_version_id=g["v1"].id,
                    new_version_id=v2.id,
                    document_id=g["document"].id,
                    content_hashes_equal=False,
                    previous_text="a",
                    new_text="b salary",
                )
    db_session.refresh(g["rule"])
    assert g["rule"].verification_status == "human_verified"


def test_direct_sql_event_forgery_blocked(db_session: Session):
    g = _seed(db_session, code="XX-E2E6")
    item = create_pending_review_item(
        db_session,
        subject_type="legal_rule",
        jurisdiction_id=g["jurisdiction"].id,
        legal_rule_id=g["rule"].id,
        document_version_id=g["v1"].id,
        review_reason="Forgery target.",
        actor=ActorRef(actor_id="service:seed", actor_kind="service"),
        risk_class="high",
    )
    append_review_event(
        db_session,
        review_item_id=item.id,
        event_type="review_started",
        actor=ActorRef(actor_id="reviewer:alice", actor_kind="human_reviewer"),
        expected_row_version=1,
    )
    # Direct INSERT must fail (insert gate), even for the migration/app login.
    with pytest.raises((DBAPIError, ProgrammingError)):
        with db_session.begin_nested():
            db_session.execute(
                text(
                    """
                    INSERT INTO legal_review_events (
                        review_item_id, event_type, from_workflow_state,
                        to_workflow_state, occurred_at, actor_id, actor_kind,
                        rationale, metadata
                    ) VALUES (
                        :iid, 'approved', 'in_review', 'approved', NOW(),
                        'attacker', 'human_reviewer', 'forged', '{}'::jsonb
                    )
                    """
                ),
                {"iid": item.id},
            )
            db_session.flush()


def test_gated_writer_function_still_works(db_session: Session):
    g = _seed(db_session, code="XX-E2E7")
    item = create_pending_review_item(
        db_session,
        subject_type="legal_rule",
        jurisdiction_id=g["jurisdiction"].id,
        legal_rule_id=g["rule"].id,
        document_version_id=g["v1"].id,
        review_reason="Gated writer path.",
        actor=ActorRef(actor_id="service:seed", actor_kind="service"),
    )
    # create_pending already used lp_insert; history must exist.
    hist = db_session.execute(
        text(
            "SELECT COUNT(*) FROM legal_review_events WHERE review_item_id = :iid"
        ),
        {"iid": item.id},
    ).scalar()
    assert int(hist or 0) >= 1


def test_legacy_json_does_not_promote(db_session: Session, tmp_path, monkeypatch):
    g = _seed(db_session, code="XX-E2E8")
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(
        '{"items":[{"id":"legacy1","jurisdiction":"XX-E2E8",'
        '"pathway_id":"xx_route","verification_status":"human_verified",'
        '"claim":"legacy"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "lifepilot.governance.high_risk_queue_import.DEFAULT_QUEUE_PATH",
        queue_path,
        raising=False,
    )
    # Import should not set LegalRule human_verified.
    try:
        import_high_risk_queue(db_session, dry_run=True, path=queue_path)
    except TypeError:
        import_high_risk_queue(db_session, dry_run=True)
    db_session.refresh(g["rule"])
    assert g["rule"].verification_status == "unverified"


def test_api_legal_review_disabled_by_default():
    from lifepilot.api.settings import get_api_settings, reset_api_settings_cache

    reset_api_settings_cache()
    settings = get_api_settings()
    assert bool(getattr(settings, "legal_review_api_enabled", False)) is False
    reset_api_settings_cache()


def test_advisory_retains_uncertainty_not_human_label(monkeypatch):
    monkeypatch.setenv("LIFEPILOT_LEGAL_VERIFICATION_POLICY", "advisory")
    reset_legal_verification_policy_cache()
    mech = _mech_evidence()
    blocked, _ = strict_mode_blocks_high_risk_claim(
        question="What is the Skilled Worker salary threshold?",
        claim_text="The Skilled Worker salary threshold is GBP 41,700.",
        evidence=[mech],
    )
    assert blocked is False
    assert mech.verification_state != EvidenceVerificationState.HUMAN_VERIFIED_RULE
    reset_legal_verification_policy_cache()


def test_rejected_claims_omitted_from_api_projection():
    from lifepilot.agent.claim_provenance import project_claim_verification_for_api
    from lifepilot.agent.verification import ClaimVerificationOutcome

    views = project_claim_verification_for_api(
        [
            ClaimVerificationOutcome(
                claim="bad",
                status=ClaimVerificationStatus.CONTRADICTED,
                retained=False,
            ),
            ClaimVerificationOutcome(
                claim="good",
                status=ClaimVerificationStatus.SUPPORTED,
                retained=True,
                evidence_ids=["ev1"],
            ),
        ],
        include_excerpts=False,
    )
    assert [v["claim"] for v in views] == ["good"]
    assert "legal_authority_label" in views[0]


def test_governance_validation_overhead_bounded(db_session: Session):
    g = _seed(db_session, code="XX-E2E9")
    from lifepilot.governance.synthetic_authority import ensure_synthetic_human_authority

    ensure_synthetic_human_authority(db_session, g["rule"], actor_id="reviewer:alice")
    started = time.perf_counter()
    for _ in range(25):
        assert validate_legal_rule_human_authority(db_session, g["rule"]).ok
    elapsed_ms = (time.perf_counter() - started) * 1000
    # Representative bound: 25 invariant checks should stay well under 2s locally.
    assert elapsed_ms < 2000, f"invariant overhead too high: {elapsed_ms:.1f}ms"


def test_curated_rule_orchestration_path(db_session: Session):
    g = _seed(db_session, code="XX-E2E10")
    from lifepilot.governance.synthetic_authority import ensure_synthetic_human_authority

    ensure_synthetic_human_authority(db_session, g["rule"], actor_id="reviewer:alice")

    class _FakeEmb:
        model_id = "fake"
        dimensions = 8

        def embed_query(self, text: str):
            return [0.01] * 8

    @contextmanager
    def _bound():
        yield db_session

    orch = EvidenceOrchestrator(session_factory=_bound, embedder=_FakeEmb())
    orch._retrieve_internal = lambda **_kw: ([], "no_results", 0.1)  # type: ignore[method-assign]
    res = orch.gather(
        question="What is the salary threshold?",
        jurisdiction=g["jurisdiction"].code,
        pathway_id="xx_route",
        as_of_date=date(2026, 6, 1),
        external_mode="off",
    )
    assert any(
        e.verification_state == EvidenceVerificationState.HUMAN_VERIFIED_RULE
        for e in res.evidence
    )
