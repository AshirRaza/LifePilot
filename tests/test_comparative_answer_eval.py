"""Deterministic tests for Phase 9D.1 comparative evaluation harness."""

from __future__ import annotations

import json
from pathlib import Path

from lifepilot.agent.contracts import AgentStatus
from lifepilot.agent.evidence import (
    EvidenceItem,
    EvidenceSourceType,
    EvidenceVerificationState,
)
from lifepilot.agent.orchestration import OrchestrationResult
from lifepilot.evaluation.comparative import (
    MODE_SPECS,
    ExternalOnlyEvidenceOrchestrator,
    classify_automated_assessment,
    retrieval_options_for_mode,
)

ROOT = Path(__file__).resolve().parents[1]


def test_original_benchmark_unchanged_shape():
    bench = json.loads(
        (ROOT / "config" / "eval" / "agent_qa_benchmark_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert bench["benchmark_id"] == "agent_qa_benchmark_v1"
    assert len(bench["cases"]) == 50
    assert {c["id"] for c in bench["cases"]} >= {"E15", "F07", "S05"}


def test_supplemental_benchmark_separate_and_has_held_out():
    supp = json.loads(
        (ROOT / "config" / "eval" / "agent_qa_supplemental_9d1.json").read_text(
            encoding="utf-8"
        )
    )
    assert supp["benchmark_id"] == "agent_qa_supplemental_9d1"
    assert len(supp["cases"]) == 20
    held = [c for c in supp["cases"] if c.get("held_out")]
    assert len(held) == 5
    assert all(c["id"].startswith("X") for c in supp["cases"])


def test_mode_retrieval_options():
    off = retrieval_options_for_mode("internal_only")
    assert off["external_mode"] == "off"
    assert off["include_external"] is False
    auto = retrieval_options_for_mode("auto_hybrid")
    assert auto["external_mode"] == "auto"
    assert auto["include_external"] is None
    assert MODE_SPECS["external_only"].external_only_filter is True


def test_external_only_orchestrator_strips_internal():
    class _Inner:
        def gather(self, **kwargs):
            assert kwargs.get("external_mode") == "on"
            return OrchestrationResult(
                evidence=[
                    EvidenceItem(
                        evidence_id="ev_internal_passage_001",
                        source_type=EvidenceSourceType.INTERNAL_RAG,
                        verification_state=EvidenceVerificationState.UNVERIFIED_RETRIEVAL,
                        jurisdiction="GB",
                        source_url="https://www.gov.uk/x",
                        passage_text="Internal curated Skilled Worker overview passage text.",
                        document_id=1,
                        document_version_id=1,
                        chunk_id=1,
                    ),
                    EvidenceItem(
                        evidence_id="ev_external_passage_001",
                        source_type=EvidenceSourceType.EXTERNAL_API,
                        verification_state=EvidenceVerificationState.UNVERIFIED_EXTERNAL,
                        jurisdiction="GB",
                        source_url="https://www.gov.uk/skilled-worker-visa",
                        passage_text="External official Skilled Worker guidance passage text.",
                        provider="govuk_content",
                    ),
                ],
                status=AgentStatus.SUCCESS,
            )

    orch = ExternalOnlyEvidenceOrchestrator(_Inner())  # type: ignore[arg-type]
    out = orch.gather(question="q", jurisdiction="GB")
    assert len(out.evidence) == 1
    assert out.evidence[0].source_type == EvidenceSourceType.EXTERNAL_API
    assert out.retrieval_decision == "external_only_diagnostic"


def test_classify_flags_eligibility_leak():
    assessment = classify_automated_assessment(
        case={
            "personal_eligibility_prohibited": True,
            "expected_behavior": "must_not_declare_personal_eligibility",
            "risk_category": "personal_eligibility",
        },
        row={
            "answer": "Based on this, you are eligible for the visa today.",
            "claims": [],
            "evidence": [],
            "warnings": [],
            "missing_information": [],
            "agent_status": "success",
        },
    )
    assert assessment["safety"]["eligibility_leak_heuristic"] is True
    assert assessment["factual_independently_verified"] is False
    assert assessment["human_verdict"] == "not_yet_verified"


def test_reference_packets_exist_for_high_risk():
    packets = json.loads(
        (ROOT / "config" / "eval" / "reference_evidence_packets_9d1.json").read_text(
            encoding="utf-8"
        )
    )
    assert packets["packets"]
    assert any("X01" in (p.get("case_ids") or []) for p in packets["packets"])
    assert all(
        p.get("verification_status")
        in {"requires_human_review", "insufficient_reference_evidence"}
        for p in packets["packets"]
    )
