"""Offline audit + re-grounding of stored Phase 9C.1 grounded Q&A results.

Preserves baseline artifacts. Writes:
  data/runs/grounded_qa_eval/phase_9c2_offline_audit.jsonl
  data/runs/grounded_qa_eval/phase_9c2_reground.jsonl

For the human-readable markdown report, run:
  python scripts/write_grounding_audit_report.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from lifepilot.agent.answer import AnswerClaim, GroundedAnswer  # noqa: E402
from lifepilot.agent.evidence import (  # noqa: E402
    EvidenceItem,
    EvidenceSourceType,
    EvidenceVerificationState,
)
from lifepilot.agent.grounding import validate_and_ground_answer  # noqa: E402

BASELINE = ROOT / "data" / "runs" / "grounded_qa_eval" / "baseline_9c1" / "results.jsonl"
OUT_DIR = ROOT / "data" / "runs" / "grounded_qa_eval"
AUDIT_PATH = OUT_DIR / "phase_9c2_offline_audit.jsonl"
REGROUND_PATH = OUT_DIR / "phase_9c2_reground.jsonl"
BENCHMARK = ROOT / "config" / "eval" / "agent_qa_benchmark_v1.json"

_NUM_RE = re.compile(
    r"(?:£|\$|€)\s?\d[\d,]*(?:\.\d+)?|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+(?:\.\d+)?\s?%|\b(?:19|20)\d{2}\b"
)
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}", re.I)


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _to_evidence_items(rows: list[dict[str, Any]]) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    for e in rows:
        source_type = EvidenceSourceType(e.get("source_type") or "internal_rag")
        ver = EvidenceVerificationState(
            e.get("verification_state") or "unverified_retrieval"
        )
        items.append(
            EvidenceItem(
                evidence_id=e["evidence_id"],
                source_type=source_type,
                jurisdiction=e.get("jurisdiction") or "XX",
                source_url=e.get("source_url"),
                document_id=e.get("document_id"),
                document_version_id=e.get("document_version_id"),
                chunk_id=e.get("chunk_id"),
                heading_path=e.get("heading_path"),
                passage_text=e.get("passage_text") or "(empty)",
                retrieval_score=e.get("retrieval_score"),
                verification_state=ver,
                applicability_limitations=e.get("applicability_limitations")
                or "Retrieved material is not validated pathway evidence.",
            )
        )
    return items


def audit_case(case: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    evid = {e["evidence_id"]: e for e in (row.get("evidence") or [])}
    claims = row.get("claims") or []
    answer = row.get("answer") or ""
    findings: list[dict[str, Any]] = []
    severities: list[str] = []

    # Claim-level support within retrieved passages
    claim_labels: list[str] = []
    for c in claims:
        passages = [
            (evid[i].get("passage_text") or "")
            for i in (c.get("evidence_ids") or [])
            if i in evid
        ]
        ct = _tokens(c.get("claim") or "")
        pt = _tokens(" ".join(passages))
        ov = ct & pt
        ratio = len(ov) / max(1, len(ct))
        nums = _NUM_RE.findall(c.get("claim") or "")
        nums_ok = True
        bad_nums = []
        joined = " ".join(passages)
        joined_c = re.sub(r"[\s,]", "", joined)
        for n in nums:
            compact = re.sub(r"[\s,]", "", n)
            digits = re.sub(r"[^\d.]", "", n)
            if compact not in joined_c and digits not in joined_c:
                nums_ok = False
                bad_nums.append(n)
        if not passages:
            label = "unsupported"
            sev = "high"
        elif not nums_ok:
            label = "unsupported"
            sev = "high"
            findings.append(
                {
                    "type": "numeric_not_in_passage",
                    "claim": c.get("claim"),
                    "evidence_ids": c.get("evidence_ids"),
                    "numbers": bad_nums,
                    "excerpt": (passages[0][:220] if passages else ""),
                    "severity": sev,
                }
            )
        elif ratio >= 0.35 and len(ov) >= 4:
            label = "supported"
            sev = "low"
        elif ratio >= 0.22:
            label = "partially_supported"
            sev = "medium"
            findings.append(
                {
                    "type": "weak_passage_overlap",
                    "claim": c.get("claim"),
                    "evidence_ids": c.get("evidence_ids"),
                    "overlap_ratio": round(ratio, 3),
                    "excerpt": (passages[0][:220] if passages else ""),
                    "severity": sev,
                }
            )
        else:
            label = "unsupported"
            sev = "high"
            findings.append(
                {
                    "type": "claim_not_supported_by_citation",
                    "claim": c.get("claim"),
                    "evidence_ids": c.get("evidence_ids"),
                    "overlap_ratio": round(ratio, 3),
                    "excerpt": (passages[0][:220] if passages else ""),
                    "severity": sev,
                }
            )
        claim_labels.append(label)
        severities.append(sev)

    # Free-text vs claims: orphan numbers in answer
    claim_blob = " ".join(c.get("claim") or "" for c in claims)
    ans_nums = _NUM_RE.findall(answer)
    orphan = []
    claim_c = re.sub(r"[\s,]", "", claim_blob)
    evid_c = re.sub(r"[\s,]", "", " ".join(e.get("passage_text") or "" for e in evid.values()))
    for n in ans_nums:
        compact = re.sub(r"[\s,]", "", n)
        digits = re.sub(r"[^\d.]", "", n)
        if compact not in claim_c and digits not in claim_c:
            if compact not in evid_c and digits not in evid_c:
                orphan.append(n)
    if orphan:
        findings.append(
            {
                "type": "answer_orphan_numbers",
                "numbers": orphan,
                "severity": "high",
            }
        )
        severities.append("high")

    dropped = [w for w in (row.get("warnings") or []) if str(w).startswith("dropped_claim:")]
    if dropped:
        findings.append(
            {
                "type": "dropped_claims_present",
                "count": len(dropped),
                "warnings": dropped[:5],
                "severity": "medium",
                "note": "Baseline kept free-text answer even when claims were dropped.",
            }
        )
        severities.append("medium")

    if re.search(r"unverified retrieval|COMPLETE pathway|grounding_dropped", answer, re.I):
        findings.append(
            {
                "type": "implementation_disclaimer_in_answer",
                "severity": "low",
            }
        )
        severities.append("low")

    # Overall classification
    if not claims and not answer:
        overall = "insufficient_evidence_to_assess"
    elif not claims:
        overall = "insufficient_evidence_to_assess"
    elif any(l == "unsupported" for l in claim_labels) or orphan:
        overall = "unsupported" if any(l == "unsupported" for l in claim_labels) else "partially_supported"
        if orphan and all(l == "supported" for l in claim_labels):
            overall = "partially_supported"
    elif any(l == "partially_supported" for l in claim_labels):
        overall = "partially_supported"
    elif claim_labels and all(l == "supported" for l in claim_labels):
        overall = "supported"
    else:
        overall = "insufficient_evidence_to_assess"

    # Completeness heuristic vs key points
    key_points = case.get("key_points") or []
    missed = []
    for kp in key_points:
        ktok = _tokens(kp)
        if not ktok:
            continue
        if len(ktok & _tokens(answer)) / len(ktok) < 0.35:
            missed.append(kp)

    worst = "none"
    if "critical" in severities:
        worst = "critical"
    elif "high" in severities:
        worst = "high"
    elif "medium" in severities:
        worst = "medium"
    elif "low" in severities:
        worst = "low"

    return {
        "question_id": case["id"],
        "question": case["question"],
        "jurisdiction": case.get("jurisdiction"),
        "category": case.get("category"),
        "original_answer": answer,
        "original_claims": claims,
        "audit_classification": overall,
        "claim_classifications": claim_labels,
        "findings": findings,
        "worst_severity": worst,
        "missed_key_points_heuristic": missed,
        "independent_legal_verification": False,
        "support_scope": "within_retrieved_evidence_only",
        "evidence_count": len(evid),
        "citation_urls": sorted(
            {e.get("source_url") for e in evid.values() if e.get("source_url")}
        ),
    }


def reground_case(row: dict[str, Any]) -> dict[str, Any]:
    evidence = _to_evidence_items(row.get("evidence") or [])
    raw = GroundedAnswer(
        answer=row.get("answer") or "No answer.",
        claims=[
            AnswerClaim(
                claim=c["claim"],
                evidence_ids=c.get("evidence_ids") or ["missing"],
                uncertainty=c.get("uncertainty"),
            )
            for c in (row.get("claims") or [])
            if c.get("claim") and c.get("evidence_ids")
        ],
        missing_information=list(row.get("missing_information") or []),
        limitations=[],
    )
    # If no claims, fabricate empty
    if not raw.claims:
        raw = GroundedAnswer(
            answer=row.get("answer") or "No answer.",
            claims=[],
            missing_information=list(row.get("missing_information") or []) or ["supported_claims"],
        )
    result = validate_and_ground_answer(
        raw,
        evidence=evidence,
        jurisdiction=row.get("jurisdiction") or "XX",
    )
    new_answer = result.answer.answer if result.answer else None
    new_claims = (
        [c.model_dump(mode="json") for c in result.answer.claims] if result.answer else []
    )
    return {
        "question_id": row["question_id"],
        "original_answer": row.get("answer"),
        "revised_answer": new_answer,
        "original_claim_count": len(row.get("claims") or []),
        "revised_claim_count": len(new_claims),
        "dropped_claims": result.dropped_claims,
        "warnings": result.warnings,
        "status_hint": result.status_hint,
        "answer_rebuilt_from_claims": result.answer_rebuilt_from_claims,
        "answer_changed": (row.get("answer") or "") != (new_answer or ""),
        "revised_claims": new_claims,
    }


def main() -> int:
    bench = json.loads(BENCHMARK.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in bench["cases"]}
    rows = [
        json.loads(line)
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    audits = []
    regrounds = []
    with AUDIT_PATH.open("w", encoding="utf-8") as fa, REGROUND_PATH.open(
        "w", encoding="utf-8"
    ) as fr:
        for row in rows:
            case = by_id[row["question_id"]]
            audit = audit_case(case, row)
            reg = reground_case(row)
            audits.append(audit)
            regrounds.append(reg)
            fa.write(json.dumps(audit, ensure_ascii=False) + "\n")
            fr.write(json.dumps(reg, ensure_ascii=False) + "\n")

    from collections import Counter

    cls = Counter(a["audit_classification"] for a in audits)
    sev = Counter(a["worst_severity"] for a in audits)
    changed = sum(1 for r in regrounds if r["answer_changed"])
    rebuilt = sum(1 for r in regrounds if r["answer_rebuilt_from_claims"])
    print("audit_classifications", dict(cls))
    print("worst_severity", dict(sev))
    print("reground_answer_changed", changed)
    print("reground_rebuilt_from_claims", rebuilt)
    print("wrote", AUDIT_PATH)
    print("wrote", REGROUND_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
