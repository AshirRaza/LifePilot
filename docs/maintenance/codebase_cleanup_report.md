# LifePilot — Codebase Cleanup Report (post–Phase 9C.2)

**Date:** 2026-11-13  
**Branch:** `chore/codebase-cleanup-post-9c2`  
**Scope:** Maintenance / simplification only. No Phase 9C.3 or 9D. No product features.

## 1. Summary

High-confidence cleanup after Phase 9C.2 grounding hardening:

- Historical phase reports organized into a local archive (not deleted).
- Essential 9C.1 / 9C.2 evaluation and audit artifacts left active and immutable.
- One phase-numbered one-off report writer renamed to a purpose-based name.
- Root DB smoke script moved under `scripts/`.
- Dead helper removed from the grounded Q&A eval runner.
- README brought in line with Claude grounded answers and current test counts.
- Alembic config updated to silence the `path_separator` deprecation warning.
- No backend architecture rewrite; no dependency removals; no schema/migration changes.

## 2. Before / after test results

| | Result |
|---|---|
| Pre-cleanup baseline | **506 passed, 5 skipped** (71.89s) |
| Post-cleanup | **506 passed, 5 skipped** (70.95s) |

No intentional behavior changes. No live Anthropic calls during routine pytest.

## 3. Files deleted

| Path | Why |
|---|---|
| _(none tracked)_ | No production modules were safe-to-delete after import/reference audit |
| Local `__pycache__/` trees (non-venv) | Regenerable; already gitignored |

`scripts/write_phase_9c2_report.py` was **renamed**, not deleted (see below).

## 4. Files renamed / moved

| Before | After | Why |
|---|---|---|
| `scripts/write_phase_9c2_report.py` | `scripts/write_grounding_audit_report.py` | Purpose-based name; still regenerates the 9C.2 markdown report from stored JSONL |
| `check_db.py` (repo root) | `scripts/check_database.py` | Keeps operational CLIs under `scripts/`; README updated |

## 5. Files / directories archived (not deleted)

Moved from `data/runs/` → `data/runs/archive/phase_history/`:

- `phase_7_report.md`
- `phase_8a_readiness_report.md` … `phase_8h_report.md`
- `phase_8d1_cleanup_report.md`
- `phase_9a_report.md`
- `phase_9b_report.md`
- `phase_9b1_latency_report.md`
- `phase_9c_report.md`
- `codebase_cleanup_report.md` (prior Nov 2026 cleanup)

Index: `data/runs/archive/README.md` and `docs/maintenance/development_history.md`.

## 6. Artifacts preserved for reproducibility (unchanged)

- `config/eval/agent_qa_benchmark_v1.json`
- `data/runs/phase_9c1_evaluation_report.md`
- `data/runs/phase_9c2_grounding_audit_report.md`
- `data/runs/grounded_qa_eval/baseline_9c1/` (**immutable**)
- `data/runs/grounded_qa_eval/results.jsonl`, `summary.json`, `preflight.json`
- `data/runs/grounded_qa_eval/phase_9c2_offline_audit.jsonl`
- `data/runs/grounded_qa_eval/phase_9c2_reground.jsonl`
- `data/runs/grounded_qa_eval/phase_9c2_paid_regression.jsonl`
- `data/runs/human_review_packets/`
- `data/runs/corpus_status_report.md`, coverage/evidence matrices, `backup_manifest.json`
- All Alembic migrations under `alembic/versions/`

## 7. Code / abstractions simplified

| Change | Detail |
|---|---|
| Removed unused `estimate_calls` | Dead helper in `scripts/run_grounded_qa_eval.py` |
| README accuracy | Removed outdated “No LLM answers yet”; updated setup/commands |
| Alembic ini | Added `path_separator = os` alongside existing `version_path_separator` |

Intentionally **not** changed:

- Agent package layout (`answer` / `generation` / `grounding` / `providers` / `orchestration`)
- Coverage subpackage split (large behavioral surface; merge risk > clarity)
- External provider class-per-API layout
- LLM `Protocol` + `DisabledLLMProvider` (needed for tests and config-off modes)
- Retrieval ranking, embeddings, source approvals, verification states
- Grounding numeric/leakage checks introduced in 9C.2

## 8. Before / after structure (affected areas)

### `data/runs/` (local)

**Before:** many `phase_*.md` reports mixed with current eval outputs.

**After:**

```text
data/runs/
  phase_9c1_evaluation_report.md
  phase_9c2_grounding_audit_report.md
  corpus_status_report.md
  coverage_matrix.csv
  evidence_matrix.csv
  blocked_evidence_recoveries.csv
  backup_manifest.json
  grounded_qa_eval/…
  human_review_packets/…
  archive/
    README.md
    phase_history/   # older phase_* reports
```

### `scripts/`

**Before:** root `check_db.py`; `write_phase_9c2_report.py`.

**After:** `scripts/check_database.py`; `scripts/write_grounding_audit_report.py`.

### `docs/`

**Before:** none.

**After:**

```text
docs/maintenance/
  codebase_cleanup_report.md   # this file
  development_history.md
```

## 9. Script classification (post-cleanup)

1. **Essential operational:** `check_database`, `run_migrate`, `import_seed_sources`, `import_registry_config`, `run_ingestion`, `run_chunk_documents`, `run_embed_chunks`, crawl/extract/discover CLIs, `ask_lifepilot`, `run_agent_evidence`, query internal/external.
2. **Reusable maintenance / evaluation:** coverage/reliability/inventory/expansion/remediation scripts, `run_grounded_qa_eval`, `audit_grounded_answers`, `run_grounding_regression`, `write_grounding_audit_report`, `run_retrieval_eval`.
3. **Historical one-off preserved for reproducibility:** archived phase reports under `data/runs/archive/` (not scripts).
4. **Obsolete scripts deleted:** none remaining after reference audit (`write_phase_9c2_*` renamed rather than removed).

## 10. Outstanding cleanup (needs human decision)

1. Whether to commit the large uncommitted Phase 9C / 9C.1 / 9C.2 feature set on this branch or a separate PR (cleanup branch currently sits atop that working tree).
2. Whether coverage pipeline packages should eventually be merged (high risk; not done here).
3. Whether `run_coverage_audit.py` / `run_migrate.py` deserve README callouts (kept; low doc refs only).
4. Whether to vendor a short `docs/` copy of critical 9C.1/9C.2 reports into git (today they live under gitignored `data/`).

## 11. Behavior changes

None intended. Verified:

- Grounding leakage / numeric validation tests still present and passing.
- Full suite: 506 passed, 5 skipped.
- No paid LLM calls during cleanup verification.
