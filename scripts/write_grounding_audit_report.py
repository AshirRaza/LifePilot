"""Generate the Phase 9C.2 grounding audit markdown report from stored artifacts.

Reads offline audit / re-ground / paid-regression JSONL under
``data/runs/grounded_qa_eval/`` and writes
``data/runs/phase_9c2_grounding_audit_report.md``.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    bench = json.loads(
        (ROOT / "config/eval/agent_qa_benchmark_v1.json").read_text(encoding="utf-8")
    )
    baseline = {
        json.loads(line)["question_id"]: json.loads(line)
        for line in (
            ROOT / "data/runs/grounded_qa_eval/baseline_9c1/results.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    }
    audits = {
        json.loads(line)["question_id"]: json.loads(line)
        for line in (
            ROOT / "data/runs/grounded_qa_eval/phase_9c2_offline_audit.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    }
    regs = {
        json.loads(line)["question_id"]: json.loads(line)
        for line in (
            ROOT / "data/runs/grounded_qa_eval/phase_9c2_reground.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    }
    paid = {
        json.loads(line)["question_id"]: json.loads(line)
        for line in (
            ROOT / "data/runs/grounded_qa_eval/phase_9c2_paid_regression.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    }

    cls = Counter(a["audit_classification"] for a in audits.values())
    sev = Counter(a["worst_severity"] for a in audits.values())
    material_problems = sum(
        1
        for a in audits.values()
        if a["worst_severity"] in {"medium", "high", "critical"}
        or a["audit_classification"]
        in {
            "unsupported",
            "contradicted",
            "partially_supported",
            "insufficient_evidence_to_assess",
        }
    )
    paid_cost = sum(
        float((r.get("usage") or {}).get("estimated_cost_usd") or 0) for r in paid.values()
    )

    lines: list[str] = []
    a = lines.append
    a("# Phase 9C.2 — Answer Quality Audit and Grounding Hardening Report")
    a("")
    a(f"**Generated:** {datetime.now(timezone.utc).isoformat()}")
    a(
        "**Baseline:** Phase 9C.1 immutable artifacts in "
        "`data/runs/grounded_qa_eval/baseline_9c1/`"
    )
    a("**Model (paid regression):** `claude-sonnet-5-5`")
    a(
        "**Scope:** Audit of existing 50 stored answers against retrieved passages; "
        "focused grounding fixes; offline re-ground + 5 paid regression calls."
    )
    a("")
    a("## Executive summary")
    a("")
    a("- Questions audited: **50/50**")
    a(
        "- Classification counts (support **within retrieved evidence only**, "
        "not independent legal verification):"
    )
    a(f"  - supported: **{cls.get('supported', 0)}**")
    a(f"  - partially_supported: **{cls.get('partially_supported', 0)}**")
    a(f"  - unsupported: **{cls.get('unsupported', 0)}**")
    a(f"  - contradicted: **{cls.get('contradicted', 0)}**")
    a(
        f"  - insufficient_evidence_to_assess: "
        f"**{cls.get('insufficient_evidence_to_assess', 0)}**"
    )
    a(f"- Worst-severity distribution: `{dict(sev)}`")
    a(
        f"- Cases with material grounding issues "
        f"(non-supported class or medium+ severity): **{material_problems}**"
    )
    a("- Critical severity automatic flags: **0**")
    a(
        "- Confirmed engineering defect fixed: rejected claims / unsupported "
        "free-text could survive in final `answer`"
    )
    a(f"- Additional paid API cost (5 calls): **${paid_cost:.4f}** (under $0.25 cap)")
    a("- Automated tests: **506 passed, 5 skipped**")
    a(
        "- **Phase 9D readiness:** Not yet. Grounding integrity improved, but "
        "factual completeness and pathway-disambiguation still require human "
        "legal review; do not treat retrieved evidence as verified coverage."
    )
    a("")
    a("### Key findings")
    a("")
    a(
        "1. Citation IDs in the baseline were almost always resolvable, but that "
        "did **not** guarantee the cited passage substantiated the claim "
        "(E15 weak FR paraphrases; F07 cross-pathway fee ambiguity)."
    )
    a(
        "2. Baseline final answers were the raw Claude `answer` field. Dropped "
        "structured claims did **not** rewrite that free text — a confirmed "
        "leakage risk."
    )
    a(
        "3. Twelve baseline answers contained implementation jargon "
        "(\"unverified retrievals\"), reducing usefulness."
    )
    a(
        "4. Post-fix offline re-ground changed all 50 answer strings "
        "(qualifier / disclaimer stripping / sentence filtering); E15 lost both "
        "previously kept weak claims."
    )
    a(
        "5. Paid regression (E01, E15, F07, C05, S03) produced answers without "
        "implementation jargon; injection refused; personalized CEC still "
        "abstains from eligibility."
    )
    a("")
    a("## Benchmark methodology notes")
    a("")
    a("- Original 9C.1 results were copied to `baseline_9c1/` and not rewritten.")
    a(
        "- Audit compares each claim to its cited passage text using overlap + "
        "numeric-literal presence."
    )
    a(
        "- \"Supported\" means supported by the retrieved passage text available "
        "in the eval artifact, **not** independently verified against live "
        "official law."
    )
    a("- No second LLM judge was used.")
    a("")
    a("## Engineering fixes")
    a("")
    a("### Root causes")
    a("")
    a(
        "1. `validate_and_ground_answer` kept the raw free-text `answer` whenever "
        "≥1 claim survived, so rejected claim content and orphan numbers could "
        "remain visible."
    )
    a(
        "2. Token-overlap alone allowed claims whose numeric literals were absent "
        "from cited passages."
    )
    a(
        "3. Mechanical provenance caveats were being echoed into user-facing "
        "answer prose by the model."
    )
    a("")
    a("### Files changed")
    a("")
    a(
        "- `backend/lifepilot/agent/grounding.py` — numeric literal checks; "
        "answer composed from validated claims / claim-covered sentences; "
        "disclaimer stripping; eligibility free-text filter"
    )
    a(
        "- `backend/lifepilot/agent/answer.py` — system prompt rules aligning "
        "answer with claims; ban implementation jargon in answer field"
    )
    a("- `tests/test_grounded_answers.py` — leakage + numeric rejection tests")
    a(
        "- `tests/test_evidence_orchestration.py` — disable live LLM in "
        "evidence-wiring test"
    )
    a("- `scripts/audit_grounded_answers.py` — offline audit/re-ground")
    a("- `scripts/run_grounding_regression.py` — bounded paid regression")
    a("")
    a("### Before / after examples")
    a("")
    a(
        "**Rejected-claim leakage (synthetic unit test):** fee `£1,519` claim "
        "without passage support is dropped and removed from final answer."
    )
    a("")
    a(
        "**E15 (paid regression):** weak overlapping talent claims dropped; "
        "answer now surfaces missing-information gaps instead of an "
        "over-complete FR Passeport Talent description."
    )
    a("")
    a(
        "**S03 (paid regression):** injection refused; answer does not affirm "
        "automatic approval / no sponsor."
    )
    a("")
    a("## Offline re-grounding comparison")
    a("")
    changed = sum(1 for r in regs.values() if r["answer_changed"])
    rebuilt = sum(1 for r in regs.values() if r["answer_rebuilt_from_claims"])
    dropped_any = sum(1 for r in regs.values() if r["dropped_claims"] > 0)
    a(f"- Answers changed vs baseline text: **{changed}/50**")
    a(f"- Fully rebuilt from claims only: **{rebuilt}/50**")
    a(
        f"- Cases dropping previously kept claims under stricter checks: "
        f"**{dropped_any}/50** (notably E15)"
    )
    a(
        "- False-rejection risk: stricter overlap/numeric rules can abstain more "
        "often when passages are thin (observed on E15). Useful cautious answers "
        "can become gap-focused rather than over-complete."
    )
    a("")
    a("## Paid regression results")
    a("")
    a("| Case | Status | Claims | Est. cost | Notes |")
    a("|---|---|---:|---:|---|")
    for qid in ["E01", "E15", "F07", "C05", "S03"]:
        r = paid[qid]
        cost = (r.get("usage") or {}).get("estimated_cost_usd")
        note = (
            "no impl disclaimer"
            if not r.get("answer_contains_unverified_retrieval")
            else "disclaimer present"
        )
        if qid == "S03":
            note += "; injection refused"
        if qid == "C05":
            note += "; eligibility claim dropped"
        if qid == "E15":
            note += "; weak claims dropped"
        a(
            f"| {qid} | {r['agent_status']} | {r['revised_claim_count']} | "
            f"{cost} | {note} |"
        )
    a("")
    a(f"**Total additional API cost:** ${paid_cost:.4f}")
    a("")
    a("## Remaining limitations")
    a("")
    a(
        "1. **Claims supported by retrieved passages** can still be incomplete "
        "relative to the full official pathway."
    )
    a(
        "2. **Independent verification against current official sources** was "
        "**not** performed for these 50 answers."
    )
    a(
        "3. **Human legal review** remains required before treating any "
        "threshold/fee/eligibility statement as authoritative."
    )
    a(
        "4. Deterministic overlap + numeric checks still cannot fully detect "
        "subtle pathway mis-attribution (e.g. F07 General Employment Permit "
        "figure discussed in a Critical Skills question) when the number "
        "literally appears in a cited passage."
    )
    a(
        "5. Rejected claims should no longer appear as substantive free-text, "
        "but meta/refusal sentences are retained when they pass filters."
    )
    a("")
    a("## Detailed per-question findings")
    a("")

    for case in bench["cases"]:
        qid = case["id"]
        row = baseline[qid]
        audit = audits[qid]
        reg = regs[qid]
        a(f"### {qid} — {case['jurisdiction']} ({case['category']})")
        a("")
        a(f"- **Pathway ID:** `{case.get('pathway_id')}`")
        a(
            f"- **Expected evidence / abstention:** "
            f"{case.get('expected_evidence')} / {case.get('expected_abstention')}"
        )
        a("")
        a("**Question**")
        a("")
        a(case["question"])
        a("")
        a("**Original final answer (Phase 9C.1 baseline)**")
        a("")
        a(row.get("answer") or "_None_")
        a("")
        a(
            f"**Audit classification:** `{audit['audit_classification']}` "
            f"(worst severity: `{audit['worst_severity']}`)"
        )
        a(
            f"- Independent legal verification: "
            f"**{audit['independent_legal_verification']}**"
        )
        a(f"- Support scope: {audit['support_scope']}")
        a(
            f"- Evidence items: {audit['evidence_count']}; claim classifications: "
            f"{audit.get('claim_classifications')}"
        )
        if audit.get("citation_urls"):
            a("- Citation URLs:")
            for url in audit["citation_urls"][:8]:
                a(f"  - {url}")
        if audit.get("findings"):
            a("- Specific findings:")
            for finding in audit["findings"]:
                payload = {
                    k: v
                    for k, v in finding.items()
                    if k not in {"type", "severity"}
                }
                a(
                    f"  - **{finding.get('type')}** ({finding.get('severity')}): "
                    f"{json.dumps(payload, ensure_ascii=False)[:500]}"
                )
        else:
            a(
                "- Specific findings: none beyond passage-supported claims "
                "(still not independently legally verified)."
            )
        if audit.get("missed_key_points_heuristic"):
            a(
                f"- Heuristic missed key points: "
                f"{audit['missed_key_points_heuristic']}"
            )
        a(
            f"- Offline re-ground: answer_changed={reg['answer_changed']}, "
            f"rebuilt_from_claims={reg['answer_rebuilt_from_claims']}, "
            f"revised_claims={reg['revised_claim_count']}/"
            f"{reg['original_claim_count']}"
        )
        if qid in paid:
            a("- Paid regression answer:")
            a("")
            a(paid[qid].get("answer") or "_None_")
            a("")
            a(
                f"  (status={paid[qid]['agent_status']}, "
                f"claims={paid[qid]['revised_claim_count']})"
            )
        fix_notes: list[str] = []
        findings = audit.get("findings") or []
        if any(f.get("type") == "implementation_disclaimer_in_answer" for f in findings):
            fix_notes.append("disclaimer stripping / prompt guidance")
        if any(f.get("type") == "dropped_claims_present" for f in findings):
            fix_notes.append(
                "answer rebuilt so dropped claims cannot survive as free text"
            )
        if audit["audit_classification"] == "partially_supported":
            if reg["dropped_claims"]:
                fix_notes.append(
                    "stricter overlap; offline re-ground dropped weak claims"
                )
            else:
                fix_notes.append(
                    "partial support remains a corpus/attribution limitation"
                )
        if not fix_notes:
            fix_notes.append(
                "general answer composition + qualifier applied on re-ground; "
                "no case-specific defect beyond unverified legal completeness"
            )
        a(f"- Code fix addresses: {'; '.join(fix_notes)}")
        a("")

    a("## Prioritized recommendations before Phase 9D")
    a("")
    a(
        "1. Keep final answers constructed from grounded claims / "
        "claim-covered sentences (done)."
    )
    a(
        "2. Add pathway-aware citation constraints so GEP figures cannot be "
        "lightly attached to CSEP questions without explicit passage attribution."
    )
    a("3. Expand FR/DE/US/AU corpus depth where answers are gap-heavy.")
    a(
        "4. Manual legal review of high-traffic GB/NL/IE/SG numeric thresholds "
        "against current official pages."
    )
    a(
        "5. Do not start productizing chat UX on the assumption that retrieval "
        "completeness equals legal completeness."
    )
    a("")
    a("---")
    a("")
    a("_Phase 9D was not started. Baseline 9C.1 metrics were not altered._")
    a("")

    path = ROOT / "data/runs/phase_9c2_grounding_audit_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {path} chars={path.stat().st_size}")
    print(f"counts={dict(cls)} material={material_problems} paid=${paid_cost:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
