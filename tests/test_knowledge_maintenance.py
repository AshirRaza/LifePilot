"""Knowledge-base maintenance pipeline tests (no live network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from lifepilot.maintenance.change_classify import ChangeClass, classify_content_change
from lifepilot.maintenance.pending_changes import (
    pending_uncertainty_reasons,
    save_pending_changes,
    upsert_pending_item,
)
from lifepilot.maintenance.pipeline import find_due_documents, run_knowledge_maintenance
from lifepilot.maintenance.review_packets import write_review_packet
from lifepilot.agent.retrieval_policy import (
    decide_retrieval,
    ExternalMode,
    reconcile_effective_dates,
)
from lifepilot.agent.evidence import (
    EvidenceItem,
    EvidenceSourceType,
    EvidenceVerificationState,
)


def test_classify_unchanged_hash():
    assert (
        classify_content_change(
            previous_text="a", new_text="b", fetch_ok=True, hashes_equal=True
        )
        == ChangeClass.UNCHANGED
    )


def test_classify_fetch_failed():
    assert (
        classify_content_change(
            previous_text="a", new_text=None, fetch_ok=False
        )
        == ChangeClass.FETCH_FAILED
    )


def test_classify_material_money_change():
    prev = "The minimum salary is £38,700 a year for this route."
    new = "The minimum salary is £41,700 a year for this route."
    assert (
        classify_content_change(previous_text=prev, new_text=new, fetch_ok=True)
        == ChangeClass.POTENTIALLY_MATERIAL
    )


def test_classify_editorial_cookie_noise():
    prev = "Eligibility overview for the permit.\nContact support for help."
    new = "Eligibility overview for the permit.\nCookie policy updated. Contact support for help."
    assert (
        classify_content_change(previous_text=prev, new_text=new, fetch_ok=True)
        == ChangeClass.EDITORIAL
    )


def test_classify_uncertain_large_delta():
    prev = "Short page."
    new = "Short page.\n" + ("Additional procedural paragraph. " * 80)
    result = classify_content_change(previous_text=prev, new_text=new, fetch_ok=True)
    assert result in {ChangeClass.UNCERTAIN, ChangeClass.EDITORIAL, ChangeClass.POTENTIALLY_MATERIAL}


def test_pending_changes_influence_retrieval(tmp_path: Path):
    path = tmp_path / "pending.json"
    save_pending_changes(
        {
            "items": [
                {
                    "id": "chg_1",
                    "jurisdiction": "GB",
                    "pathway_id": "gb_skilled_worker",
                    "status": "pending_review",
                    "change_class": "potentially_material",
                }
            ]
        },
        path=path,
    )
    reasons = pending_uncertainty_reasons(
        jurisdiction="GB", pathway_id="gb_skilled_worker", path=path
    )
    assert reasons
    # Unrelated pathway not blocked.
    assert not pending_uncertainty_reasons(
        jurisdiction="GB", pathway_id="gb_student", path=path
    )


def test_review_packet_not_human_verified(tmp_path: Path):
    path = write_review_packet(
        {
            "id": "pkt_test",
            "jurisdiction": "FR",
            "pathway_id": "fr_passeport_talent",
            "claim": "E15 coverage",
            "change_class": "uncertain",
            "status": "pending_review",
            "recommended_action": "Review official overview",
        },
        directory=tmp_path,
    )
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["human_verified"] is False


def test_future_effective_still_flagged():
    ev = EvidenceItem(
        evidence_id="ev_future_rule_0001",
        source_type=EvidenceSourceType.INTERNAL_RAG,
        verification_state=EvidenceVerificationState.UNVERIFIED_RETRIEVAL,
        jurisdiction="GB",
        source_url="https://www.gov.uk/x",
        passage_text="The new fee is effective from 1 January 2099 under transitional rules.",
        document_id=1,
        document_version_id=1,
        chunk_id=1,
    )
    notes = reconcile_effective_dates([ev])
    assert notes.future_effective_mentions


def test_historical_question_internal_only_with_pending():
    decision = decide_retrieval(
        question="What was the Skilled Worker salary threshold in 2020?",
        jurisdiction="GB",
        pathway_id="gb_skilled_worker",
        external_mode=ExternalMode.AUTO,
        substantive_internal_count=3,
        internal_status="ok",
        freshness=None,
        connectors_available=True,
    )
    assert decision.decision.value == "internal_only"


def test_find_due_documents_respects_last_checked(monkeypatch):
    now = datetime.now(timezone.utc)
    doc = SimpleNamespace(id=1, canonical_url="https://www.gov.uk/skilled-worker-visa", primary_source_id=9)
    ver = SimpleNamespace(
        id=2,
        retrieved_at=now - timedelta(days=30),
        last_checked_at=now - timedelta(hours=1),
    )

    class _Sess:
        def scalars(self, *_a, **_k):
            return [doc]

    policy = {
        "categories": {"salary_thresholds_financial": {"recheck_hours": 24}},
        "priority_sources": [
            {
                "jurisdiction": "GB",
                "pathway_id": "gb_skilled_worker",
                "category": "salary_thresholds_financial",
                "url_contains": "/skilled-worker-visa",
            }
        ],
    }

    monkeypatch.setattr(
        "lifepilot.maintenance.pipeline.get_current_version",
        lambda session, document_id: ver,
    )
    due = find_due_documents(_Sess(), policy)  # type: ignore[arg-type]
    assert due == []


def test_maintenance_dry_run_idempotent(monkeypatch):
    now = datetime.now(timezone.utc)
    doc = SimpleNamespace(
        id=11,
        canonical_url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant",
        primary_source_id=3,
    )
    ver = SimpleNamespace(id=22, retrieved_at=now - timedelta(days=10), last_checked_at=None)

    class _Sess:
        def scalars(self, *_a, **_k):
            return [doc]

        def get(self, model, pk):
            if model.__name__ == "Source":
                return SimpleNamespace(id=pk, review_status="approved", name="IND")
            return None

    monkeypatch.setattr(
        "lifepilot.maintenance.pipeline.get_current_version",
        lambda session, document_id: ver,
    )
    policy = {
        "categories": {"salary_thresholds_financial": {"recheck_hours": 24}},
        "priority_sources": [
            {
                "jurisdiction": "NL",
                "pathway_id": "nl_highly_skilled_migrant",
                "category": "salary_thresholds_financial",
                "url_contains": "ind.nl",
            }
        ],
    }
    monkeypatch.setattr(
        "lifepilot.maintenance.pipeline._load_policy",
        lambda path: policy,
    )
    r1 = run_knowledge_maintenance(_Sess(), dry_run=True, limit=5)  # type: ignore[arg-type]
    r2 = run_knowledge_maintenance(_Sess(), dry_run=True, limit=5)  # type: ignore[arg-type]
    assert r1.counts == r2.counts
    assert r1.items
    assert all(i["status"] == "dry_run" for i in r1.items)


def test_upsert_pending_idempotent(tmp_path: Path):
    path = tmp_path / "p.json"
    upsert_pending_item(
        {"id": "a", "jurisdiction": "SG", "status": "pending_review"}, path=path
    )
    upsert_pending_item(
        {"id": "a", "jurisdiction": "SG", "status": "pending_review", "note": "x"},
        path=path,
    )
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["items"]) == 1
    assert data["items"][0]["note"] == "x"
