"""Artifact paths and the markdown report for Phase 6H."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from lifepilot.coverage.pathway_inventory.report import write_csv, write_json  # re-exported

__all__ = ["default_6h_paths", "write_csv", "write_json", "write_report_md"]


def default_6h_paths(runs_dir: Path) -> dict[str, Path]:
    return {
        "baseline": runs_dir / "phase_6h_baseline.json",
        "approval_decisions": runs_dir / "phase_6h_source_approval_decisions.csv",
        "ingestion_report": runs_dir / "phase_6h_ingestion_report.json",
        "document_relevance": runs_dir / "phase_6h_document_relevance.csv",
        "canada_evidence": runs_dir / "phase_6h_canada_program_evidence.csv",
        "evidence_changes": runs_dir / "phase_6h_evidence_changes.csv",
        "country_coverage": runs_dir / "phase_6h_country_coverage.csv",
        "storage_integrity": runs_dir / "phase_6h_storage_integrity.json",
        "pending_approvals": runs_dir / "phase_6h_pending_approvals.csv",
        "priority_backlog": runs_dir / "phase_6h_priority_backlog.csv",
        "readiness": runs_dir / "phase_6h_readiness.json",
        "report": runs_dir / "phase_6h_report.md",
    }


def _trunc(value: Any, n: int = 110) -> str:
    s = str(value if value is not None else "").replace("|", "/").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def write_report_md(
    path: Path,
    *,
    baseline: dict[str, Any],
    ingestion: dict[str, Any],
    decisions: list[dict[str, Any]],
    approval_actions: dict[str, Any],
    relevance_rows: list[dict[str, Any]],
    evidence_summary: dict[str, Any],
    change_summary: dict[str, Any],
    evidence_changes: list[dict[str, Any]],
    evidence_summary_6g: dict[str, Any],
    country_rows: list[dict[str, Any]],
    entity_reconciliation: dict[str, Any],
    metrics_totals: dict[str, Any],
    canada_rows: list[dict[str, Any]],
    canada_differences: dict[str, Any],
    storage: dict[str, Any],
    readiness: dict[str, Any],
    pending_rows: list[dict[str, Any]],
    test_results: dict[str, Any],
    fingerprint_ok: bool,
) -> None:
    L: list[str] = []
    live0 = baseline.get("live_database") or {}
    after = ingestion.get("after_counts") or {}
    by_id = {d["candidate_id"]: d for d in decisions}

    L += ["# Phase 6H \u2014 Owner-Approved Expansion, Evidence Deepening and Corrected Metrics", ""]
    L += [
        "Scope: owner-authorized exact sources only; no wildcard or whole-domain approval; no "
        "substitute URLs fetched; readiness is automated/provisional, never human-verified. "
        "No RAG, APIs or frontend.",
        "",
    ]

    # 1
    L += ["## 1. Baseline versus final corpus", ""]
    L += ["| Metric | 6G expected | Baseline (pre-write) | Final | Delta |", "|---|---:|---:|---:|---:|"]
    for k in ("approved_sources", "documents", "document_versions", "success_extractions"):
        exp = (baseline.get("expected_from_6g") or {}).get(k, "")
        b = live0.get(k, "")
        f = after.get(k, "")
        d = (f - b) if isinstance(f, int) and isinstance(b, int) else ""
        L.append(f"| {k} | {exp} | {b} | {f} | {d} |")
    L += [
        "",
        f"Baseline `captured_before_any_phase_writes={baseline.get('captured_before_any_phase_writes')}` "
        f"(captured {baseline.get('captured_at')}); never overwritten.",
        "",
    ]
    totals = ingestion.get("totals") or {}
    lim = ingestion.get("limits") or {}
    L += [
        f"Fetch attempts: **{totals.get('fetches_attempted', 0)}** of {lim.get('max_fetch_attempts', 100)} "
        f"(new versions {totals.get('fetches_new_version', 0)}, failed {totals.get('fetches_failed', 0)}, "
        f"blocked {totals.get('fetches_blocked', 0)}); network budget remaining "
        f"{totals.get('remaining_network_budget', '')} of {lim.get('max_network_attempts', 200)}.",
        f"Unused budget: {(ingestion.get('unused_budget') or {}).get('reason', '')}",
        "",
    ]

    # 2
    L += ["## 2. Source approval decisions (all, including rejected)", ""]
    L += ["| Candidate | Decision | Code | HTTP | Reason |", "|---|---|---|---:|---|"]
    for d in decisions:
        L.append(
            f"| {d['candidate_id']} | {d['decision']} | {d['decision_code']} | {d.get('http_status') or ''} "
            f"| {_trunc(d.get('reason'), 120)} |"
        )
    L += ["", "Registry state (source, status now, created / last updated):"]
    for d in decisions:
        if d.get("registry_source_name"):
            L.append(
                f"- `{d['candidate_id']}` \u2192 source {d['source_id']} "
                f"\"{d['registry_source_name']}\" status `{d['registry_source_status_now']}`; "
                f"created {d['registry_source_created_at']}, updated {d['registry_source_updated_at']}"
            )
    L += ["", "Registry actions reported by the final run (idempotent; an earlier run may have "
          "committed the change \u2014 see timestamps above):"]
    for cid, a in approval_actions.items():
        L.append(
            f"- `{cid}` \u2192 source {a.get('source_id')}: {a.get('action')} "
            f"(patterns after: `{json.dumps(a.get('patterns_after'), ensure_ascii=False)}`)"
        )
    L.append("")

    # 3
    L += ["## 3. New sources and documents by country", ""]
    newv = ingestion.get("new_document_versions") or []
    by_country = Counter(v.get("country") or "?" for v in newv)
    L += ["| Country | New document versions |", "|---|---:|"]
    for c, n in sorted(by_country.items()):
        L.append(f"| {c} | {n} |")
    L.append("")
    for v in newv:
        L.append(
            f"- [{v.get('country')}] doc {v['document_id']} v{v.get('version_number')} "
            f"(version {v['version_id']}, {v.get('content_kind')}, HTTP {v.get('http_status')}) "
            f"{v['canonical_url']}"
        )
    L.append("")

    # 4
    L += ["## 4. Extraction successes and failures", ""]
    ex = Counter(str(v.get("extraction_status") or "none") for v in newv)
    L.append(f"- New-version extraction status: `{dict(ex)}`")
    d_ext = (ingestion.get("delta") or {}).get("success_extractions")
    if isinstance(d_ext, int) and d_ext > len(newv):
        L.append(
            f"- Extraction delta ({d_ext}) exceeds new versions ({len(newv)}): the "
            f"process-existing pass (no network) also extracted {d_ext - len(newv)} previously "
            "stored but un-extracted Phase 6G USCIS versions (docs 2255-2260) in the touched "
            "source 9080."
        )
    for run in ingestion.get("per_source_runs") or []:
        L.append(
            f"- source {run['source_id']} ({run['key']}): fetched {run['fetches_attempted']}, "
            f"new {run['fetches_new_version']}, failed {run['fetches_failed']}, "
            f"blocked {run['fetches_blocked']}; extractions ok {run['extractions_success']} / "
            f"failed {run['extractions_failed']} / empty {run['extractions_empty']}"
        )
        for f in run.get("failures") or []:
            L.append(f"  - failure: {_trunc(json.dumps(f, ensure_ascii=False, default=str), 200)}")
    for t in ingestion.get("targeted_exact_fetches") or []:
        L.append(
            f"- targeted exact fetch [{t['country']}] {t['url']}: status `{t.get('status')}` "
            f"(HTTP {t.get('http_status')}, doc {t.get('document_id')})"
        )
    for s in ingestion.get("skipped") or []:
        L.append(f"- skipped `{s['key']}`: {s['reason']}")
    for v in newv:
        if v.get("extraction_status") not in (None, "success"):
            L.append(
                f"- extraction `{v.get('extraction_status')}` doc {v['document_id']}: "
                f"{_trunc(v.get('extraction_message'), 160)}"
            )
    L.append("")

    # 5
    L += ["## 5. Evidence cells promoted, downgraded or unchanged", ""]
    L.append(f"True-pathway cells by change type: `{json.dumps(change_summary)}`")
    L.append("")
    for kind in ("promoted", "downgraded"):
        items = [c for c in evidence_changes if c["entity_type"] == "true_pathway" and c["change"] == kind]
        L.append(f"**{kind} ({len(items)})**")
        for c in items[:40]:
            L.append(
                f"- `{c['pathway_id']}` / {c['requirement']}: {c['classification_6g']} \u2192 "
                f"{c['classification_6h']} ({c['evidence_repair']})"
            )
        L.append("")
    newly = [c for c in evidence_changes if c["entity_type"] == "true_pathway" and c["change"] == "newly_assessed"]
    L.append(f"**newly assessed from UNEVALUATED ({len(newly)})** \u2014 see `phase_6h_evidence_changes.csv`.")
    L.append("")

    # 6
    L += ["## 6. UNEVALUATED reduction and remaining gaps", ""]
    pub = baseline.get("expected_from_6g") or {}
    cls6g = {
        "COMPLETE": pub.get("complete", 0), "PARTIAL": pub.get("partial", 0),
        "MISSING": pub.get("missing", 0), "UNEVALUATED": pub.get("unevaluated", 0), "BLOCKED": 0,
    }
    u6g = pub.get("applicable_cells")
    cls6h = evidence_summary.get("classification_counts_applicable") or {}
    L += ["| Classification | 6G (published) | 6H |", "|---|---:|---:|"]
    for k in ("COMPLETE", "PARTIAL", "MISSING", "UNEVALUATED", "BLOCKED"):
        L.append(f"| {k} | {cls6g.get(k, 0)} | {cls6h.get(k, 0)} |")
    L += [
        "",
        f"Applicable cells: 6G {u6g} \u2192 6H {evidence_summary.get('applicable_cells')}; "
        f"COMPLETE rate {evidence_summary.get('complete_rate_applicable')}.",
        "",
        "Why UNEVALUATED/MISSING remains: most pathways still have no approved pathway-specific "
        "official page (AE/JP/FR, many GB/US/AU/SG routes); hubs are not evidence; auto-matching "
        "can never exceed PARTIAL; JP engineer.html (404) and FR N19804 (hub) were rejected and not "
        "replaced.",
        "",
    ]

    # 7
    L += ["## 7. Canada Express Entry (independent FSW / CEC / FST)", ""]
    L += ["| Program | eligibility | required_documents | application_process | financial | provenance |", "|---|---|---|---|---|---|"]
    cls = (canada_differences or {}).get("classifications") or {}
    for pid, req in cls.items():
        L.append(
            f"| {pid} | {req.get('eligibility')} | {req.get('required_documents')} | "
            f"{req.get('application_process')} | {req.get('financial_or_sponsorship')} | "
            f"{req.get('official_provenance')} |"
        )
    L += ["", "Program differences found in the stored IRCC pages:", ""]
    for pid, f in ((canada_differences or {}).get("facts") or {}).items():
        L.append(f"- **{pid}**: " + "; ".join(f"{k}: {v}" for k, v in f.items()))
    L.append("")
    notes = [r for r in canada_rows if r.get("source_notes")]
    for r in notes:
        L.append(f"- Note ({r['pathway_id']}): {r['source_notes']}")
    miss = {(r["pathway_id"], r["requirement"]): r["missing_subrequirements"] for r in canada_rows if r["missing_subrequirements"]}
    for (pid, req), m in miss.items():
        L.append(f"- Missing for `{pid}` / {req}: {m}")
    L.append("")

    # 8
    L += ["## 8. Storage integrity findings", ""]
    L += [
        f"- Versions in DB: {storage.get('document_versions')}; files referenced: "
        f"{storage.get('files_referenced_by_db')}; files on disk: {storage.get('raw_files_on_disk')}",
        f"- Missing files: {len(storage.get('missing_files') or [])}; size mismatches: "
        f"{len(storage.get('size_mismatches') or [])}; orphan files: {storage.get('orphan_file_count')}",
        f"- SHA-256 verified {storage.get('sha256_verified')}/{storage.get('sha256_sample_size')} "
        f"({storage.get('sample_policy')}); mismatches: {len(storage.get('sha256_mismatches') or [])}",
        f"- Integrity OK: **{storage.get('integrity_ok')}**",
    ]
    for f in storage.get("findings") or []:
        L.append(f"- [{f['severity']}] {f['finding']}")
    L.append("- Remediation (proposal only, nothing executed):")
    for r in storage.get("remediation_proposal_only") or []:
        L.append(f"  - {r}")
    L.append("")

    # 9
    L += ["## 9. AE / JP / FR outcomes", ""]
    for cid in ("AE-uae-portal-visa-emirates-id", "JP-isa-engineer-specialist", "FR-service-public-n19804"):
        d = by_id.get(cid)
        if d:
            L.append(
                f"- **{cid}**: {d['decision']} / `{d['decision_code']}` \u2014 {_trunc(d.get('reason'), 220)}"
            )
    ae_docs = [v for v in newv if v.get("country") == "AE"]
    L.append(f"- AE documents ingested: {len(ae_docs)} (hub-level; AE true pathways are not claimed complete).")
    cr = {r["country"]: r for r in country_rows}
    for c in ("AE", "JP", "FR"):
        r = cr.get(c)
        if r:
            L.append(
                f"- {c}: approved source in country = {r['country_has_approved_source']}; "
                f"pathway_source_linked {r['pathway_source_linked']}/{r['true_pathways']}; "
                f"pathway-relevant docs extracted {r['pathway_relevant_document_extracted']}"
            )
    L.append("")

    # 10
    L += ["## 10. Ready and human-verified pathways", ""]
    L += [
        f"- Provisionally ready (automated): `{readiness.get('provisionally_ready_pathways')}`",
        f"- Newly provisionally ready: `{readiness.get('newly_provisionally_ready')}`; "
        f"lost from 6G: `{readiness.get('lost_from_6g')}`",
        f"- Human-verified: `{readiness.get('human_verified_ready_pathways')}` (none; 6H cannot verify)",
        "",
    ]

    # 11
    L += ["## 11. Corrected metrics", ""]
    L.append(f"- Entity reconciliation: `{entity_reconciliation.get('formula')}` "
             f"(total {entity_reconciliation.get('total_entities')}, matches 149: "
             f"{entity_reconciliation.get('matches_expected_149')})")
    nat = metrics_totals.get("national") or {}
    eu = metrics_totals.get("eu_framework") or {}
    L.append(f"- National: `{json.dumps(nat)}`")
    L.append(f"- EU framework (separate): `{json.dumps(eu)}`")
    L += ["", "| Country | True | country_has_approved_source | pathway_source_linked | relevant ingested | relevant extracted | legacy source-wide |", "|---|---:|---|---:|---:|---:|---:|"]
    for r in country_rows:
        L.append(
            f"| {r['country']} | {r['true_pathways']} | {r['country_has_approved_source']} | "
            f"{r['pathway_source_linked']} | {r['pathway_relevant_document_ingested']} | "
            f"{r['pathway_relevant_document_extracted']} | {r['legacy_source_wide_ingested_pathways']} |"
        )
    L.append("")

    # 12
    L += ["## 12. Tests", ""]
    L.append(f"- Command: `{test_results.get('command')}`")
    L.append(f"- Result: **{test_results.get('summary')}**")
    L.append(f"- Prior-phase artifact fingerprint unchanged: **{fingerprint_ok}**")
    L.append("")

    # 13
    L += ["## 13. Remaining approval requests and recommended next batch", ""]
    L.append(f"Pending items: **{len(pending_rows)}** (see `phase_6h_pending_approvals.csv`). Highest priority:")
    for a in sorted(pending_rows, key=lambda r: int(r.get("priority") or 3))[:20]:
        L.append(
            f"- [{a.get('jurisdiction')}] `{a.get('pathway')}` \u2014 {a.get('url') or '(research needed)'} "
            f"({a.get('approval_status')})"
        )
    L += [
        "",
        "Recommended next batch (requires separate approval; not started):",
        "1. JP: research the current ISA status page for Engineer/Specialist; FR: a passeport-talent specific official page.",
        "2. AE: exact-URL approval for employment, student and family residence pages; GB/US/AU: pathway-specific sub-pages.",
        "3. Canada: research the exact IRCC post-invitation document checklist page to close required_documents.",
        "4. Human-verify provisionally ready pathways before any production use.",
        "5. Implement the storage backup/verify proposal.",
        "6. Do not begin 6I without approval.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")
