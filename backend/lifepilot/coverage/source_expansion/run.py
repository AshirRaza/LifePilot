"""CLI entry for Phase 6H (owner-approved expansion, evidence deepening, corrected metrics)."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from lifepilot.coverage.normalization.artifacts import default_runs_dir
from lifepilot.coverage.pathway_inventory.inventory import build_inventory_rows
from lifepilot.coverage.pathway_inventory.evidence import build_evidence_rows
from lifepilot.coverage.source_expansion.approve import apply_decisions
from lifepilot.coverage.source_expansion.canada import (
    assess_canada,
    load_canada_records,
    program_differences,
)
from lifepilot.coverage.source_expansion.candidates import (
    authorized_candidates,
    build_pending_approvals,
)
from lifepilot.coverage.source_expansion.coverage import (
    build_country_coverage_6h,
    build_priority_backlog_6h,
)
from lifepilot.coverage.source_expansion.evidence import (
    build_evidence_6h,
    evidence_summary_6h,
    summarize_changes,
)
from lifepilot.coverage.source_expansion.ingest import INGEST_PLAN, run_bounded_ingestion
from lifepilot.coverage.source_expansion.metrics import (
    build_country_metrics,
    build_link_overlay,
    build_pathway_metrics,
    load_approved_sources,
    load_corpus_documents,
    metrics_totals,
    pathway_document_matches,
    reconcile_entities,
)
from lifepilot.coverage.source_expansion.readiness import build_readiness_6h
from lifepilot.coverage.source_expansion.relevance import (
    SIXG_NEW_DOC_ID_RANGE,
    classify_ingested_document,
    load_current_extractions,
    load_documents_by_ids,
    near_duplicate_groups,
)
from lifepilot.coverage.source_expansion.report import (
    default_6h_paths,
    write_csv,
    write_json,
    write_report_md,
)
from lifepilot.coverage.source_expansion.storage_audit import audit_storage
from lifepilot.coverage.source_expansion.validate import decide, load_probe, recheck_robots
from lifepilot.db.models import Source
from lifepilot.db.session import get_session_factory

PRESERVED = (
    "phase_6e_coverage_inventory.json",
    "phase_6e_coverage_matrix.csv",
    "phase_6e_gap_register.csv",
    "phase_6e_country_summary.md",
    "phase_6e_acceptance_readiness.json",
    "phase_6e1_normalized_pathways.json",
    "phase_6e1_calibrated_coverage_matrix.csv",
    "phase_6e1_review_queue.csv",
    "phase_6e1_corrected_gap_register.csv",
    "phase_6e1_readiness_report.md",
    "phase_6f0_evidence_review.csv",
    "phase_6f0_reconciliation.json",
    "phase_6f0_readiness.json",
    "phase_6f0_remediation_plan.json",
    "phase_6f0_report.md",
    "phase_6f1_baseline.json",
    "phase_6f1_evidence_repairs.csv",
    "phase_6f1_extraction_repairs.json",
    "phase_6f1_source_proposals.csv",
    "phase_6f1_ingestion_report.json",
    "phase_6f1_readiness.json",
    "phase_6f1_report.md",
    "phase_6f2_baseline.json",
    "phase_6f2_approved_ingestion.json",
    "phase_6f2_evidence_repairs.csv",
    "phase_6f2_source_proposals.csv",
    "phase_6f2_readiness.json",
    "phase_6f2_report.md",
    "phase_6g_baseline.json",
    "phase_6g_pathway_inventory.csv",
    "phase_6g_country_coverage.csv",
    "phase_6g_source_discovery.csv",
    "phase_6g_approval_queue.csv",
    "phase_6g_ingestion_report.json",
    "phase_6g_evidence_coverage.csv",
    "phase_6g_readiness.json",
    "phase_6g_priority_backlog.csv",
    "phase_6g_report.md",
)

PROBE_FILE = "_probe_6h_url_validation.json"


def fingerprint(runs_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in PRESERVED:
        path = runs_dir / name
        if path.is_file():
            out[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def run_tests() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[4]
    py = root / ".venv" / "Scripts" / "python.exe"
    cmd = [
        str(py if py.is_file() else "python"),
        "-m",
        "pytest",
        "tests/test_source_expansion.py",
        "tests/test_pathway_inventory.py",
        "tests/test_source_gap_closure.py",
        "tests/test_allowlist_exact_discovery.py",
        "-q",
        "--tb=line",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(root))
    summary = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else f"exit={proc.returncode}"
    return {
        "command": " ".join(cmd[1:]),
        "exit_code": proc.returncode,
        "summary": summary if proc.returncode == 0 else f"FAILED {summary}",
        "details": (proc.stdout or "")[-1200:],
        "executed": True,
    }


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _blocked_pathways(
    decisions: list[dict[str, Any]],
    pathway_metrics: list[dict[str, Any]],
    ingestion: dict[str, Any],
    source_by_candidate: dict[str, int],
) -> dict[str, str]:
    """Approved candidates whose fetch was blocked/failed and left no pathway page."""
    pm = {p["pathway_id"]: p for p in pathway_metrics}
    runs = {r["source_id"]: r for r in ingestion.get("per_source_runs") or []}
    out: dict[str, str] = {}
    for d in decisions:
        if d.get("decision") != "approved":
            continue
        sid = source_by_candidate.get(d["candidate_id"])
        run = runs.get(sid)
        if not run:
            continue
        if not (run.get("fetches_blocked") or run.get("fetches_failed")):
            continue
        for pid in str(d.get("pathway_ids") or "").split(","):
            p = pm.get(pid)
            if pid and p and not (p["pathway_strong_document_ids"] or p["pathway_shared_document_ids"]):
                out[pid] = (
                    f"source {sid}: blocked={run.get('fetches_blocked')} failed={run.get('fetches_failed')} "
                    f"notes={run.get('notes')}"
                )
    return out


def run_source_expansion(
    *,
    runs_dir: Path | None = None,
    skip_ingestion: bool = False,
    dry_run_ingestion: bool = False,
    skip_tests: bool = False,
    reuse_ingestion_report: bool = False,
) -> dict[str, Path]:
    root_runs = runs_dir or default_runs_dir()
    paths = default_6h_paths(root_runs)
    before_fp = fingerprint(root_runs)

    if not paths["baseline"].is_file():
        raise FileNotFoundError(
            f"Missing pre-write baseline {paths['baseline']}. Capture it before any writes."
        )
    baseline_bytes_sha = hashlib.sha256(paths["baseline"].read_bytes()).hexdigest()
    baseline = json.loads(paths["baseline"].read_text(encoding="utf-8"))
    if not baseline.get("captured_before_any_phase_writes"):
        raise RuntimeError("phase_6h_baseline.json missing captured_before_any_phase_writes=true")

    assessed_at = datetime.now(timezone.utc).isoformat()
    factory = get_session_factory()
    candidates = authorized_candidates()

    # ---- 1. validate (probe JSON + robots-only recheck for unknown robots) -------------
    probe_path = root_runs / PROBE_FILE
    probe = load_probe(probe_path)
    robots_rechecks = 0
    decisions = []
    with factory() as session:
        for c in candidates:
            pr = probe.get(c.authorized_url)
            recheck = None
            if pr is not None and (pr.get("robots") or {}).get("can_fetch") is None:
                recheck = recheck_robots(c.authorized_url)
                robots_rechecks += 1
            patterns: list[str] | None = None
            if c.source_id:
                src = session.get(Source, c.source_id)
                patterns = list(src.allowed_url_patterns or []) if src else []
            decisions.append(
                decide(c, pr, source_patterns=patterns, robots_recheck=recheck, decided_at=assessed_at)
            )

    # ---- 2. apply approvals (DB writes; rejected candidates never touch the DB) ---------
    dry = dry_run_ingestion
    with factory() as session:
        actions = apply_decisions(session, candidates, decisions)
        if dry:
            session.rollback()
        else:
            session.commit()
    approved_ids = {d.candidate_id for d in decisions if d.decision == "approved"}
    source_by_candidate = {
        cid: a.source_id for cid, a in actions.items() if a.source_id is not None
    }
    for d in decisions:
        if d.candidate_id in source_by_candidate and d.source_id is None:
            d.source_id = source_by_candidate[d.candidate_id]
    decision_rows = [d.to_row() for d in decisions]
    with factory() as session:
        src_meta = {
            int(r["id"]): dict(r)
            for r in session.execute(
                text(
                    "SELECT id, name, review_status, created_at, updated_at "
                    "FROM sources WHERE id = ANY(:ids)"
                ),
                {"ids": sorted({int(i) for i in source_by_candidate.values()}) or [0]},
            ).mappings()
        }
    for r in decision_rows:
        a = actions.get(r["candidate_id"])
        r["registry_action_this_run"] = a.action if a else "none (rejected; no DB change)"
        r["allowlist_patterns_after"] = json.dumps(a.patterns_after if a else [], ensure_ascii=False)
        meta = src_meta.get(int(r["source_id"])) if r.get("source_id") not in ("", None) else None
        r["registry_source_name"] = meta["name"] if meta else ""
        r["registry_source_status_now"] = meta["review_status"] if meta else ""
        r["registry_source_created_at"] = meta["created_at"].isoformat() if meta else ""
        r["registry_source_updated_at"] = meta["updated_at"].isoformat() if meta else ""
        r["registry_note"] = (
            "'already_registered' means an earlier run of this phase already committed the "
            "registry change (see source created/updated timestamps); changes are idempotent."
            if a and a.action in {"already_registered", "approved_existing_source"}
            else ""
        )
    write_csv(paths["approval_decisions"], decision_rows)

    # ---- 3. bounded ingestion ----------------------------------------------------------
    existing_report = (
        json.loads(paths["ingestion_report"].read_text(encoding="utf-8"))
        if paths["ingestion_report"].is_file()
        else None
    )
    if reuse_ingestion_report:
        if not existing_report or not existing_report.get("execute") or existing_report.get("ingestion_skipped"):
            raise RuntimeError("--reuse-ingestion-report needs a prior executed ingestion report")
        ingestion = existing_report
        ingestion["reused_by_later_analysis_run"] = True
    elif skip_ingestion:
        ingestion = {
            "phase": "6H", "execute": False, "ingestion_skipped": True, "skipped": [],
            "before_counts": baseline.get("live_database"),
            "after_counts": baseline.get("live_database"),
            "delta": {"approved_sources": 0, "documents": 0, "document_versions": 0, "success_extractions": 0},
            "totals": {"fetches_attempted": 0, "fetches_new_version": 0},
            "new_document_versions": [], "per_source_runs": [],
            "summary": "Ingestion skipped by flag",
        }
    else:
        ingestion = run_bounded_ingestion(
            source_by_candidate=source_by_candidate,
            approved_candidate_ids=approved_ids,
            execute=not dry_run_ingestion,
        )
    if not reuse_ingestion_report:
        ingestion.setdefault("totals", {})["validation_robots_rechecks"] = robots_rechecks
        ingestion["totals"]["network_attempts_including_validation"] = (
            (ingestion["totals"].get("fetches_attempted", 0) or 0)
            + (ingestion["totals"].get("sitemap_fetches", 0) or 0)
            + len(ingestion.get("per_source_runs") or [])
            + robots_rechecks
        )
        ingestion["plan_keys"] = [p["key"] for p in INGEST_PLAN]
        write_json(paths["ingestion_report"], ingestion)

    # ---- 4. analysis on the post-ingestion corpus --------------------------------------
    inventory_rows = build_inventory_rows()
    entity_rec = reconcile_entities(inventory_rows)
    with factory() as session:
        docs = load_corpus_documents(session)
        sources = load_approved_sources(session)
        cur_versions = {
            int(r[0]): int(r[1])
            for r in session.execute(
                text("SELECT document_id, id FROM document_versions WHERE is_current")
            ).all()
        }
    approved_source_ids = {int(s["id"]) for s in sources if s["review_status"] == "approved"}
    overlay = build_link_overlay(decision_rows, approved_source_ids)
    doc_matches = pathway_document_matches(docs, inventory_rows)
    pathway_metrics = build_pathway_metrics(
        inventory_rows, approved_source_ids=approved_source_ids,
        overlay=overlay, doc_matches=doc_matches,
    )
    country_metrics = build_country_metrics(inventory_rows, pathway_metrics, sources)
    totals_metrics = metrics_totals(country_metrics)

    new_versions = ingestion.get("new_document_versions") or []
    new_doc_ids = {int(v["document_id"]) for v in new_versions}

    # document relevance (6G USCIS/IE docs + every document added in 6H)
    lo, hi = SIXG_NEW_DOC_ID_RANGE
    rel_ids = sorted(set(range(lo, hi + 1)) | new_doc_ids)
    intended_by_source: dict[int, list[str]] = {}
    for cand in candidates:
        sid = source_by_candidate.get(cand.candidate_id)
        if sid is not None and cand.candidate_id in approved_ids:
            intended_by_source.setdefault(sid, []).extend(cand.pathway_ids)
    hub_seeds = {
        str(c.authorized_url).rstrip("/") for c in candidates if c.new_source is not None
    }
    strong_ids = sorted({d for p in pathway_metrics for d in p["pathway_strong_document_ids"]})
    with factory() as session:
        rel_docs = load_documents_by_ids(session, rel_ids)
        extractions = load_current_extractions(session, sorted(set(rel_ids) | set(strong_ids)))
        canada_records = load_canada_records(session)
    dup = near_duplicate_groups(rel_docs)
    relevance_rows: list[dict[str, Any]] = []
    for d in rel_docs:
        row = classify_ingested_document(
            d, inventory_rows=inventory_rows, extraction=extractions.get(d["document_id"]),
            intended_pathways=sorted(set(intended_by_source.get(d.get("source_id"), []))) or None,
            hub_seed_urls=hub_seeds, duplicate_of=dup.get(d["document_id"]),
        )
        row["ingested_in_phase"] = "6H" if d["document_id"] in new_doc_ids else "6G"
        relevance_rows.append(row)
    write_csv(paths["document_relevance"], relevance_rows)

    # Canada (independent programs)
    canada_rows = assess_canada(canada_records, assessed_at=assessed_at, source_approved=9083 in approved_source_ids)
    canada_diff = program_differences(canada_rows)
    write_csv(paths["canada_evidence"], canada_rows)

    # evidence overlay
    base_rows = build_evidence_rows(ica_doc_id=2252, inventory_rows=inventory_rows)
    ev_6g = evidence_summary_6h(base_rows)
    blocked = _blocked_pathways(decision_rows, pathway_metrics, ingestion, source_by_candidate)
    published_6g = {
        (r["pathway_id"], r["requirement"]): r["classification"]
        for r in _read_csv(root_runs / "phase_6g_evidence_coverage.csv")
    }
    evidence_rows, changes = build_evidence_6h(
        base_rows=base_rows, pathway_metrics=pathway_metrics, extractions=extractions,
        current_version_by_doc=cur_versions, canada_rows=canada_rows,
        new_doc_ids=new_doc_ids, blocked_pathways=blocked, assessed_at=assessed_at,
        published_6g=published_6g,
    )
    ev_sum = evidence_summary_6h(evidence_rows)
    change_summary = summarize_changes(changes)
    write_csv(paths["evidence_changes"], changes)

    prior_country = _read_csv(root_runs / "phase_6g_country_coverage.csv")
    for r in prior_country:
        for k in ("true_pathways_with_approved_source", "true_pathways_with_ingested_docs"):
            r[k] = int(r[k]) if str(r.get(k, "")).isdigit() else r.get(k)
    country_rows = build_country_coverage_6h(country_metrics, evidence_rows, prior_country)
    write_csv(paths["country_coverage"], country_rows)

    # pending approvals (deduped by normalized URL + pathway scope)
    candidate_sources = [
        {"id": s["id"], "name": s["name"], "seed_url": s["seed_url"], "country_code": s["country_code"]}
        for s in sources
        if s["review_status"] == "candidate"
    ]
    pending = build_pending_approvals(
        decisions=decision_rows, inventory=inventory_rows,
        candidate_sources=candidate_sources,
        prior_queue_rows=_read_csv(root_runs / "phase_6g_approval_queue.csv"),
    )
    pending.append(
        {
            "jurisdiction": "CA",
            "pathway": "ca_federal_skilled_worker,ca_canadian_experience_class,ca_federal_skilled_trades",
            "url": "",
            "normalized_url": "",
            "approval_status": "pending_url_research",
            "reason": (
                "Close required_documents: research the exact IRCC post-invitation document "
                "checklist page; no URL asserted or fetched."
            ),
            "origin": "phase_6h_canada",
            "priority": 1,
            "merged_origins": "phase_6h_canada",
        }
    )
    write_csv(paths["pending_approvals"], pending)
    backlog = build_priority_backlog_6h(country_rows, pending)
    write_csv(paths["priority_backlog"], backlog)

    # storage audit (read-only)
    repo_root = Path(__file__).resolve().parents[4]
    with factory() as session:
        storage = audit_storage(session, repo_root=repo_root)
    write_json(paths["storage_integrity"], storage)

    readiness = build_readiness_6h(
        evidence_rows, evidence_summary=ev_sum, change_summary=change_summary,
        entity_reconciliation=entity_rec, metrics_totals=totals_metrics,
        ingestion=ingestion,
        canada_summary={
            "classifications": canada_diff["classifications"],
            "program_facts": canada_diff["facts"],
        },
        storage=storage, baseline=baseline, decisions=decision_rows,
    )
    write_json(paths["readiness"], readiness)

    test_results = (
        {"command": "skipped", "summary": "skipped", "executed": False}
        if skip_tests
        else run_tests()
    )

    after_fp = fingerprint(root_runs)
    changed = [n for n, d in before_fp.items() if after_fp.get(n) != d]
    baseline_after = hashlib.sha256(paths["baseline"].read_bytes()).hexdigest()
    write_report_md(
        paths["report"], baseline=baseline, ingestion=ingestion, decisions=decision_rows,
        approval_actions={k: v.to_dict() for k, v in actions.items()},
        relevance_rows=relevance_rows, evidence_summary=ev_sum, change_summary=change_summary,
        evidence_changes=changes, evidence_summary_6g=ev_6g, country_rows=country_rows,
        entity_reconciliation=entity_rec, metrics_totals=totals_metrics,
        canada_rows=canada_rows, canada_differences=canada_diff, storage=storage,
        readiness=readiness, pending_rows=pending, test_results=test_results,
        fingerprint_ok=not changed and baseline_after == baseline_bytes_sha,
    )
    if changed:
        raise RuntimeError("Prior phase artifacts modified unexpectedly: " + ", ".join(changed))
    if baseline_after != baseline_bytes_sha:
        raise RuntimeError("phase_6h_baseline.json was modified")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 6H owner-approved expansion and evidence deepening"
    )
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--skip-ingestion", action="store_true")
    parser.add_argument("--dry-run-ingestion", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument(
        "--reuse-ingestion-report",
        action="store_true",
        help="Re-run analysis/report only; reuse the executed ingestion report (no network).",
    )
    args = parser.parse_args(argv)
    paths = run_source_expansion(
        runs_dir=args.runs_dir,
        skip_ingestion=args.skip_ingestion,
        dry_run_ingestion=args.dry_run_ingestion,
        skip_tests=args.skip_tests,
        reuse_ingestion_report=args.reuse_ingestion_report,
    )
    print("Phase 6H complete.")
    for k, p in paths.items():
        print(f"  {k}: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
