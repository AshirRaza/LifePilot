"""Phase 9D.3 verification reliability evaluation.

Runs fixtures through the production ``verify_material_claims`` implementation.
Does not reimplement verification logic.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lifepilot.agent.answer import AnswerClaim
from lifepilot.agent.evidence import (
    EvidenceItem,
    evidence_from_external_item,
    evidence_from_internal_passage,
)
from lifepilot.agent.pathway_applicability import (
    normalize_numeric_literal,
    numeric_literals_in_text,
)
from lifepilot.agent.verification import (
    ClaimVerificationStatus,
    verify_material_claims,
)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FIXTURE_PATH = ROOT / "config" / "eval" / "verification_reliability.json"

# Unsafe statuses for precision/recall of "should suppress / not present as fact".
UNSAFE_EXPECTED = {
    "contradicted",
    "insufficient_evidence",
    "pathway_mismatch",
}
# temporal_uncertainty is a soft qualify — scored separately.


@dataclass
class CaseResult:
    fixture_id: str
    kind: str
    category: str
    reference_status: str
    expected_statuses: list[str]
    actual_statuses: list[str]
    expected_retained: list[bool]
    actual_retained: list[bool]
    status_match: bool
    retained_match: bool
    forbidden_leaks: list[str]
    missing_required: list[str]
    answer: str
    outcomes: list[dict[str, Any]]
    passed: bool
    notes: list[str] = field(default_factory=list)


def load_fixture_suite(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_FIXTURE_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def _build_evidence(raw_items: list[dict[str, Any]]) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    for i, raw in enumerate(raw_items):
        jur = (raw.get("jurisdiction") or "GB").upper()
        url = raw.get("source_url") or f"https://example.test/synthetic/{i}"
        text = raw.get("passage_text") or ""
        source = (raw.get("source_type") or "internal").lower()
        if source == "external":
            items.append(
                evidence_from_external_item(
                    jurisdiction=jur,
                    official_url=url,
                    provider=raw.get("provider") or "fixture",
                    passage_text=text,
                    title=raw.get("title"),
                )
            )
        else:
            items.append(
                evidence_from_internal_passage(
                    jurisdiction=jur,
                    official_url=url,
                    document_id=int(raw.get("document_id") or 9000 + i),
                    document_version_id=int(raw.get("document_version_id") or 9100 + i),
                    chunk_id=int(raw.get("chunk_id") or i + 1),
                    content_text=text,
                    score=0.9,
                    heading_path=raw.get("heading_path"),
                )
            )
    return items


def _build_claims(
    raw_claims: list[dict[str, Any]], evidence: list[EvidenceItem]
) -> list[AnswerClaim]:
    claims: list[AnswerClaim] = []
    for raw in raw_claims:
        idxs = raw.get("evidence_index") or raw.get("evidence_indexes") or [0]
        eids = [evidence[int(i)].evidence_id for i in idxs]
        claims.append(
            AnswerClaim(
                claim=raw["claim"],
                evidence_ids=eids,
                uncertainty=raw.get("uncertainty"),
            )
        )
    return claims


def run_fixture(fixture: dict[str, Any]) -> CaseResult:
    evidence = _build_evidence(list(fixture.get("evidence") or []))
    claims = _build_claims(list(fixture.get("claims") or []), evidence)
    result = verify_material_claims(
        answer=fixture.get("raw_answer") or " ".join(c.claim for c in claims),
        claims=claims,
        evidence=evidence,
        jurisdiction=str(fixture.get("jurisdiction") or "GB"),
        pathway_id=fixture.get("pathway_id"),
        question=str(fixture.get("question") or ""),
        missing_information=list(fixture.get("missing_information") or []),
        max_external_corroborations=0,
    )

    expected_statuses = [str(s) for s in (fixture.get("expected_claim_statuses") or [])]
    expected_retained = [bool(x) for x in (fixture.get("expected_retained") or [])]
    actual_statuses = [o.status.value for o in result.outcomes]
    actual_retained = [bool(o.retained) for o in result.outcomes]

    # Pad/truncate expected to outcome length for partial fixtures.
    if not expected_retained and expected_statuses:
        expected_retained = [
            s
            not in {
                ClaimVerificationStatus.CONTRADICTED.value,
                ClaimVerificationStatus.PATHWAY_MISMATCH.value,
                ClaimVerificationStatus.INSUFFICIENT_EVIDENCE.value,
            }
            or s == ClaimVerificationStatus.TEMPORAL_UNCERTAINTY.value
            for s in expected_statuses
        ]
        # insufficient numeric should be false; temporal true — refine:
        expected_retained = []
        for s in expected_statuses:
            if s in {
                ClaimVerificationStatus.CONTRADICTED.value,
                ClaimVerificationStatus.PATHWAY_MISMATCH.value,
                ClaimVerificationStatus.INSUFFICIENT_EVIDENCE.value,
            }:
                expected_retained.append(False)
            else:
                expected_retained.append(True)

    status_match = actual_statuses == expected_statuses
    retained_match = (
        actual_retained == expected_retained
        if expected_retained
        else True
    )

    answer = result.answer or ""
    answer_compact = normalize_numeric_literal(answer)
    forbidden = [str(x) for x in (fixture.get("forbidden_in_answer") or [])]
    leaks = [
        f
        for f in forbidden
        if normalize_numeric_literal(f) in answer_compact or f.lower() in answer.lower()
    ]
    required = [str(x) for x in (fixture.get("required_in_answer") or [])]
    missing_req = [
        r
        for r in required
        if normalize_numeric_literal(r) not in answer_compact
        and r.lower() not in answer.lower()
    ]

    notes: list[str] = []
    if fixture.get("reference_status") in {
        "unverified",
        "requires_human_review",
        "machine_captured_unverified",
    }:
        # Soft scoring: still check status expectations when provided.
        notes.append("reference_not_human_verified")

    passed = status_match and retained_match and not leaks and not missing_req
    return CaseResult(
        fixture_id=str(fixture.get("id")),
        kind=str(fixture.get("kind") or "synthetic"),
        category=str(fixture.get("category") or ""),
        reference_status=str(fixture.get("reference_status") or "synthetic_stipulated"),
        expected_statuses=expected_statuses,
        actual_statuses=actual_statuses,
        expected_retained=expected_retained,
        actual_retained=actual_retained,
        status_match=status_match,
        retained_match=retained_match,
        forbidden_leaks=leaks,
        missing_required=missing_req,
        answer=answer,
        outcomes=[o.model_dump(mode="json") for o in result.outcomes],
        passed=passed,
        notes=notes,
    )


def summarize_results(results: list[CaseResult]) -> dict[str, Any]:
    by_kind: dict[str, list[CaseResult]] = {"synthetic": [], "real_official": []}
    for r in results:
        by_kind.setdefault(r.kind, []).append(r)

    def _block(items: list[CaseResult]) -> dict[str, Any]:
        if not items:
            return {"n": 0}
        # Unsafe detection: expected unsafe vs predicted unsafe (not retained or unsafe status)
        tp = fp = tn = fn = 0
        for r in items:
            for exp, act, exp_ret, act_ret in zip(
                r.expected_statuses,
                r.actual_statuses,
                r.expected_retained,
                r.actual_retained,
                strict=False,
            ):
                exp_unsafe = exp in UNSAFE_EXPECTED or exp_ret is False
                act_unsafe = act in UNSAFE_EXPECTED or act_ret is False
                if exp_unsafe and act_unsafe:
                    tp += 1
                elif not exp_unsafe and act_unsafe:
                    fp += 1
                elif not exp_unsafe and not act_unsafe:
                    tn += 1
                else:
                    fn += 1
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        return {
            "n": len(items),
            "passed": sum(1 for r in items if r.passed),
            "failed": sum(1 for r in items if not r.passed),
            "pass_rate": round(sum(1 for r in items if r.passed) / len(items), 4),
            "unsafe_detection": {
                "tp": tp,
                "fp": fp,
                "tn": tn,
                "fn": fn,
                "precision": None if precision is None else round(precision, 4),
                "recall": None if recall is None else round(recall, 4),
                "false_positive_rate": (
                    None if (fp + tn) == 0 else round(fp / (fp + tn), 4)
                ),
                "false_negative_rate": (
                    None if (fn + tp) == 0 else round(fn / (fn + tp), 4)
                ),
            },
            "status_match_rate": round(
                sum(1 for r in items if r.status_match) / len(items), 4
            ),
            "leak_failures": sum(1 for r in items if r.forbidden_leaks),
            "categories": dict(Counter(r.category for r in items)),
        }

    failures = [
        {
            "id": r.fixture_id,
            "kind": r.kind,
            "category": r.category,
            "expected": r.expected_statuses,
            "actual": r.actual_statuses,
            "leaks": r.forbidden_leaks,
            "missing_required": r.missing_required,
            "answer_preview": (r.answer or "")[:240],
        }
        for r in results
        if not r.passed
    ]
    return {
        "totals": {
            "n": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
        },
        "synthetic": _block(by_kind.get("synthetic") or []),
        "real_official": _block(by_kind.get("real_official") or []),
        "failures": failures,
    }


def run_suite(path: Path | None = None) -> tuple[list[CaseResult], dict[str, Any]]:
    suite = load_fixture_suite(path)
    results = [run_fixture(f) for f in suite.get("fixtures") or []]
    return results, summarize_results(results)
