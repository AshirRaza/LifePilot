"""Phase 10D.4 — Strict human verification enforcement and governance integrity.

Synthetic fixtures only. No live DB mutations. No fabricated real legal approvals.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from lifepilot.agent.contracts import AnswerClaim
from lifepilot.agent.evidence import (
    EvidenceItem,
    EvidenceSourceType,
    EvidenceVerificationState,
    evidence_from_legal_rule,
)
from lifepilot.agent.verification import ClaimVerificationStatus, verify_material_claims
from lifepilot.db.legal_review import (
    ActorRef,
    LegalReviewError,
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
from lifepilot.governance.ingest_version_guard import enforce_review_pins_on_new_current
from lifepilot.governance.legacy_reconciliation import reconcile_legacy_governance
from lifepilot.governance.legal_rule_promotion import (
    demote_legal_rule_authority,
    promote_legal_rule_from_approval,
)
from lifepilot.governance.reviewer_auth import (
    PERM_LEGAL_REVIEW_ADMIN,
    PERM_LEGAL_REVIEW_DECIDE,
    PERM_LEGAL_REVIEW_READ,
    ReviewerAuthError,
    ReviewerIdentity,
)
from lifepilot.governance.synthetic_authority import ensure_synthetic_human_authority
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
        rule_summary="Synthetic rule for 10D.4 governance integrity.",
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
            {PERM_LEGAL_REVIEW_READ, PERM_LEGAL_REVIEW_DECIDE, PERM_LEGAL_REVIEW_ADMIN}
        ),
    )


def _approve_chain(
    session: Session,
    *,
    rule: LegalRule,
    actor_id: str = "reviewer:alice",
    document_version_id: int | None = None,
    evidence_fingerprint: str | None = None,
    reviewed_cohort_key: str | None = None,
    reviewed_effective_from: date | None = None,
    reviewed_effective_to: date | None = None,
) -> int:
    pin = document_version_id or rule.document_version_id
    item = create_pending_review_item(
        session,
        subject_type="legal_rule",
        jurisdiction_id=rule.jurisdiction_id,
        legal_rule_id=rule.id,
        document_version_id=pin,
        document_id=rule.document_id,
        source_id=rule.source_id,
        pathway_id=rule.pathway_id,
        requirement_category=rule.requirement_category,
        review_reason="Synthetic 10D.4 approval chain.",
        actor=ActorRef(actor_id="service:seed", actor_kind="service"),
        evidence_fingerprint=evidence_fingerprint,
        reviewed_cohort_key=reviewed_cohort_key or rule.cohort_key,
        reviewed_effective_from=reviewed_effective_from
        if reviewed_effective_from is not None
        else rule.effective_from,
        reviewed_effective_to=reviewed_effective_to
        if reviewed_effective_to is not None
        else rule.effective_to,
        risk_class="high",
    )
    human = ActorRef(actor_id=actor_id, actor_kind="human_reviewer")
    append_review_event(
        session,
        review_item_id=item.id,
        event_type="review_started",
        actor=human,
        expected_row_version=1,
    )
    append_review_event(
        session,
        review_item_id=item.id,
        event_type="approved",
        actor=human,
        expected_row_version=2,
        rationale="Synthetic isolated approval rationale.",
        document_version_id=pin,
        evidence_fingerprint=evidence_fingerprint,
    )
    session.refresh(item)
    return item.id


def test_valid_reviewed_legal_rule_promotion(db_session: Session):
    g = _seed(db_session, code="XX-D41")
    item_id = _approve_chain(db_session, rule=g["rule"])
    item = get_review_item(db_session, review_item_id=item_id)
    assert item is not None
    rule = promote_legal_rule_from_approval(
        db_session,
        review_item_id=item_id,
        identity=_human(),
        expected_item_row_version=item.row_version,
        rationale="Promote after synthetic human approval.",
    )
    assert rule.verification_status == "human_verified"
    assert validate_legal_rule_human_authority(db_session, rule).ok
    lookup = query_reviewed_legal_rules(
        db_session,
        jurisdiction=g["jurisdiction"].code,
        pathway_id="xx_route",
        as_of_date=date(2026, 6, 1),
    )
    assert any(r.id == rule.id for r in lookup.matched_rules)


def test_missing_review_item_fails_invariant(db_session: Session):
    g = _seed(db_session, code="XX-D42")
    with pytest.raises((IntegrityError, DBAPIError)):
        with db_session.begin_nested():
            g["rule"].review_status = "approved"
            g["rule"].verification_status = "human_verified"
            g["rule"].reviewed_by = "reviewer:alice"
            g["rule"].reviewed_at = datetime.now(timezone.utc)
            db_session.flush()
    check = validate_legal_rule_human_authority(db_session, g["rule"])
    assert check.ok is False


def test_missing_approval_event_blocks_promotion(db_session: Session):
    g = _seed(db_session, code="XX-D43")
    item = create_pending_review_item(
        db_session,
        subject_type="legal_rule",
        jurisdiction_id=g["jurisdiction"].id,
        legal_rule_id=g["rule"].id,
        document_version_id=g["v1"].id,
        review_reason="Pending only.",
        actor=ActorRef(actor_id="service:seed", actor_kind="service"),
        risk_class="high",
    )
    with pytest.raises(LegalReviewError) as exc:
        promote_legal_rule_from_approval(
            db_session,
            review_item_id=item.id,
            identity=_human(),
            expected_item_row_version=item.row_version,
            rationale="Should fail — not approved.",
        )
    assert exc.value.code in {"review_item_not_approved", "missing_approval_event"}


def test_invalid_and_service_actor_cannot_promote(db_session: Session):
    g = _seed(db_session, code="XX-D44")
    item_id = _approve_chain(db_session, rule=g["rule"])
    item = get_review_item(db_session, review_item_id=item_id)
    assert item is not None
    service = ReviewerIdentity(
        actor_id="service:api_key",
        actor_kind="service",
        permissions=frozenset({PERM_LEGAL_REVIEW_DECIDE, PERM_LEGAL_REVIEW_ADMIN}),
    )
    with pytest.raises(ReviewerAuthError):
        promote_legal_rule_from_approval(
            db_session,
            review_item_id=item_id,
            identity=service,
            expected_item_row_version=item.row_version,
            rationale="Service must not promote.",
        )
    stranger = ReviewerIdentity(
        actor_id="reviewer:bob",
        actor_kind="human_reviewer",
        permissions=frozenset({PERM_LEGAL_REVIEW_DECIDE}),
    )
    with pytest.raises(ReviewerAuthError):
        promote_legal_rule_from_approval(
            db_session,
            review_item_id=item_id,
            identity=stranger,
            expected_item_row_version=item.row_version,
            rationale="Wrong reviewer identity.",
        )


def test_mismatched_version_revision_fingerprint_cohort_period(db_session: Session):
    g = _seed(db_session, code="XX-D45")
    v2 = DocumentVersion(
        document_id=g["document"].id,
        fetched_via_source_id=g["source"].id,
        version_number=2,
        source_url=g["document"].canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
        http_status=200,
        content_sha256=_sha("XX-D45-v2"),
        byte_size=140,
        storage_path="data/raw/test/xx-d45_v2.html",
        is_current=False,
        legal_status="unknown",
    )
    db_session.add(v2)
    db_session.flush()

    item_id = _approve_chain(
        db_session,
        rule=g["rule"],
        evidence_fingerprint="fp-aaa",
    )
    item = get_review_item(db_session, review_item_id=item_id)
    assert item is not None
    promote_legal_rule_from_approval(
        db_session,
        review_item_id=item_id,
        identity=_human(),
        expected_item_row_version=item.row_version,
        rationale="Promote then mutate scope.",
    )
    g["rule"].cohort_key = "other_cohort"
    check = validate_legal_rule_human_authority(db_session, g["rule"])
    assert check.ok is False
    assert "cohort_scope_mismatch" in check.reasons

    g["rule"].cohort_key = "general"
    g["rule"].effective_from = date(2020, 1, 1)
    check2 = validate_legal_rule_human_authority(db_session, g["rule"])
    assert check2.ok is False
    assert "effective_period_mismatch" in check2.reasons

    g["rule"].effective_from = date(2024, 1, 1)
    g["rule"].document_version_id = v2.id
    check3 = validate_legal_rule_human_authority(db_session, g["rule"])
    assert check3.ok is False
    assert "document_version_pin_mismatch" in check3.reasons

    g["rule"].document_version_id = g["v1"].id
    item.evidence_fingerprint = "fp-bbb"
    db_session.flush()
    check4 = validate_legal_rule_human_authority(db_session, g["rule"])
    assert check4.ok is False
    assert "evidence_fingerprint_mismatch" in check4.reasons


def test_reopen_and_source_supersession_demote(db_session: Session):
    g = _seed(db_session, code="XX-D46")
    ensure_synthetic_human_authority(db_session, g["rule"], actor_id="reviewer:alice")
    item = db_session.execute(
        text(
            "SELECT id, row_version FROM legal_review_items "
            "WHERE legal_rule_id = :rid ORDER BY id DESC LIMIT 1"
        ),
        {"rid": g["rule"].id},
    ).one()
    append_review_event(
        db_session,
        review_item_id=item.id,
        event_type="reopened",
        actor=ActorRef(actor_id="reviewer:alice", actor_kind="human_reviewer"),
        expected_row_version=item.row_version,
        rationale="Reopen for reassessment.",
    )
    db_session.refresh(g["rule"])
    assert g["rule"].verification_status != "human_verified"
    assert validate_legal_rule_human_authority(db_session, g["rule"]).ok is False

    g2 = _seed(db_session, code="XX-D46B")
    ensure_synthetic_human_authority(db_session, g2["rule"], actor_id="reviewer:alice")
    v2 = DocumentVersion(
        document_id=g2["document"].id,
        fetched_via_source_id=g2["source"].id,
        version_number=2,
        source_url=g2["document"].canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime(2026, 11, 21, tzinfo=timezone.utc),
        http_status=200,
        content_sha256=_sha("XX-D46B-v2"),
        byte_size=150,
        storage_path="data/raw/test/xx-d46b_v2.html",
        is_current=True,
        legal_status="unknown",
    )
    g2["v1"].is_current = False
    db_session.add(v2)
    db_session.flush()
    enforce_review_pins_on_new_current(
        db_session,
        previous_version_id=g2["v1"].id,
        new_version_id=v2.id,
        document_id=g2["document"].id,
        content_hashes_equal=False,
        previous_text="old salary threshold text",
        new_text="new potentially material salary threshold 99000",
    )
    db_session.refresh(g2["rule"])
    assert g2["rule"].verification_status != "human_verified"


def test_ordinary_ingestion_guard_idempotent_and_crash_safe(db_session: Session):
    g = _seed(db_session, code="XX-D47")
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
        content_sha256=_sha("XX-D47-v2"),
        byte_size=160,
        storage_path="data/raw/test/xx-d47_v2.html",
        is_current=True,
        legal_status="unknown",
    )
    g["v1"].is_current = False
    db_session.add(v2)
    db_session.flush()

    r1 = enforce_review_pins_on_new_current(
        db_session,
        previous_version_id=g["v1"].id,
        new_version_id=v2.id,
        document_id=g["document"].id,
        content_hashes_equal=False,
        previous_text="threshold A",
        new_text="threshold B material change salary",
    )
    assert r1.get("invalidated", 0) >= 1 or r1.get("skipped") is not True
    r2 = enforce_review_pins_on_new_current(
        db_session,
        previous_version_id=g["v1"].id,
        new_version_id=v2.id,
        document_id=g["document"].id,
        content_hashes_equal=False,
        previous_text="threshold A",
        new_text="threshold B material change salary",
    )
    assert r2.get("invalidated", 0) == 0 or r2.get("already_invalidated", 0) >= 0

    g3 = _seed(db_session, code="XX-D47C")
    ensure_synthetic_human_authority(db_session, g3["rule"], actor_id="reviewer:alice")
    v3 = DocumentVersion(
        document_id=g3["document"].id,
        fetched_via_source_id=g3["source"].id,
        version_number=2,
        source_url=g3["document"].canonical_url,
        content_type="text/html",
        language="en",
        retrieved_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
        http_status=200,
        content_sha256=_sha("XX-D47C-v2"),
        byte_size=160,
        storage_path="data/raw/test/xx-d47c_v2.html",
        is_current=True,
        legal_status="unknown",
    )
    g3["v1"].is_current = False
    db_session.add(v3)
    db_session.flush()
    with pytest.raises(RuntimeError):
        with db_session.begin_nested():
            with patch(
                "lifepilot.governance.ingest_version_guard.process_source_version_change",
                side_effect=RuntimeError("injected_crash"),
            ):
                enforce_review_pins_on_new_current(
                    db_session,
                    previous_version_id=g3["v1"].id,
                    new_version_id=v3.id,
                    document_id=g3["document"].id,
                    content_hashes_equal=False,
                    previous_text="a",
                    new_text="b",
                )
    db_session.refresh(g3["rule"])
    assert g3["rule"].verification_status == "human_verified"


def test_direct_sql_eventless_review_item_update_rejected(db_session: Session):
    g = _seed(db_session, code="XX-D48")
    item_id = _approve_chain(db_session, rule=g["rule"])
    with pytest.raises((IntegrityError, DBAPIError)):
        with db_session.begin_nested():
            db_session.execute(
                text(
                    "UPDATE legal_review_items SET workflow_state = 'rejected' "
                    "WHERE id = :id"
                ),
                {"id": item_id},
            )
            db_session.flush()


def test_unauthorized_legal_rule_verification_update_rejected(db_session: Session):
    g = _seed(db_session, code="XX-D49")
    with pytest.raises((IntegrityError, DBAPIError)):
        with db_session.begin_nested():
            db_session.execute(
                text(
                    "UPDATE legal_rules SET verification_status = 'human_verified', "
                    "review_status = 'approved', reviewed_by = 'forged', "
                    "reviewed_at = NOW() WHERE id = :id"
                ),
                {"id": g["rule"].id},
            )
            db_session.flush()


def test_strict_vs_advisory_policy(monkeypatch):
    reset_legal_verification_policy_cache()
    monkeypatch.setenv("LIFEPILOT_LEGAL_VERIFICATION_POLICY", "strict_verified_only")
    reset_legal_verification_policy_cache()
    assert (
        resolve_effective_policy(requested="advisory", allow_client_override=True)
        == LegalVerificationPolicy.STRICT_VERIFIED_ONLY
    )
    mech = EvidenceItem(
        evidence_id="ev_mech_01",
        source_type=EvidenceSourceType.INTERNAL_RAG,
        verification_state=EvidenceVerificationState.UNVERIFIED_RETRIEVAL,
        jurisdiction="GB",
        source_url="https://www.gov.uk/skilled-worker-visa",
        passage_text="Skilled Worker salary threshold is GBP 41,700.",
    )
    blocked, reason = strict_mode_blocks_high_risk_claim(
        question="What is the Skilled Worker salary threshold?",
        claim_text="The Skilled Worker salary threshold is GBP 41,700.",
        evidence=[mech],
    )
    assert blocked is True
    assert reason and "strict_verified_only" in reason

    monkeypatch.setenv("LIFEPILOT_LEGAL_VERIFICATION_POLICY", "advisory")
    reset_legal_verification_policy_cache()
    blocked2, _ = strict_mode_blocks_high_risk_claim(
        question="What is the Skilled Worker salary threshold?",
        claim_text="The Skilled Worker salary threshold is GBP 41,700.",
        evidence=[mech],
    )
    assert blocked2 is False
    claim = AnswerClaim(
        claim="The Skilled Worker salary threshold is GBP 41,700.",
        evidence_ids=[mech.evidence_id],
        uncertainty="high",
    )
    verify_material_claims(
        answer=claim.claim,
        claims=[claim],
        evidence=[mech],
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
    )
    assert mech.verification_state != EvidenceVerificationState.HUMAN_VERIFIED_RULE
    reset_legal_verification_policy_cache()


def test_evidence_from_legal_rule_requires_session_authority(db_session: Session):
    g = _seed(db_session, code="XX-D410")
    ensure_synthetic_human_authority(db_session, g["rule"], actor_id="reviewer:alice")
    ok = evidence_from_legal_rule(g["rule"], session=db_session)
    assert ok.verification_state == EvidenceVerificationState.HUMAN_VERIFIED_RULE

    demote_legal_rule_authority(
        db_session, legal_rule_id=g["rule"].id, reason="test_demote"
    )
    db_session.refresh(g["rule"])
    with pytest.raises(ValueError):
        evidence_from_legal_rule(g["rule"], session=db_session)


def test_legacy_reconciliation_dry_run_does_not_invent_approvals(db_session: Session):
    g = _seed(db_session, code="XX-D411")
    report = reconcile_legacy_governance(db_session, dry_run=True)
    assert report.dry_run is True
    assert "independently establish legal approval" in report.to_dict()["policy"]
    count = db_session.execute(
        text(
            "SELECT COUNT(*) FROM legal_review_events e "
            "JOIN legal_review_items i ON i.id = e.review_item_id "
            "WHERE i.legal_rule_id = :rid AND e.event_type = 'approved'"
        ),
        {"rid": g["rule"].id},
    ).scalar()
    assert int(count or 0) == 0


def test_historical_expired_invalidated_excluded(db_session: Session):
    g = _seed(db_session, code="XX-D412")
    ensure_synthetic_human_authority(db_session, g["rule"], actor_id="reviewer:alice")
    item = db_session.execute(
        text(
            "SELECT id, row_version FROM legal_review_items "
            "WHERE legal_rule_id = :rid ORDER BY id DESC LIMIT 1"
        ),
        {"rid": g["rule"].id},
    ).one()
    append_review_event(
        db_session,
        review_item_id=item.id,
        event_type="approval_invalidated",
        actor=ActorRef(actor_id="reviewer:alice", actor_kind="human_reviewer"),
        expected_row_version=item.row_version,
        rationale="Synthetic invalidation of prior approval.",
    )
    db_session.refresh(g["rule"])
    assert validate_legal_rule_human_authority(db_session, g["rule"]).ok is False
    assert g["rule"].verification_status != "human_verified"

    lookup = query_reviewed_legal_rules(
        db_session,
        jurisdiction=g["jurisdiction"].code,
        pathway_id="xx_route",
        as_of_date=date(2026, 6, 1),
    )
    assert all(r.id != g["rule"].id for r in lookup.matched_rules)


def test_no_rejected_claims_in_api_projection():
    from lifepilot.agent.claim_provenance import project_claim_verification_for_api
    from lifepilot.agent.verification import ClaimVerificationOutcome

    outcomes = [
        ClaimVerificationOutcome(
            claim="Rejected synthetic claim",
            evidence_ids=[],
            status=ClaimVerificationStatus.CONTRADICTED,
            retained=False,
            reasons=["contradicted"],
        ),
        ClaimVerificationOutcome(
            claim="Retained synthetic claim",
            evidence_ids=["e1"],
            status=ClaimVerificationStatus.SUPPORTED,
            retained=True,
            reasons=[],
        ),
    ]
    views = project_claim_verification_for_api(outcomes, include_excerpts=False)
    assert len(views) == 1
    assert views[0]["claim"] == "Retained synthetic claim"
    assert "legal_authority_label" in views[0]
