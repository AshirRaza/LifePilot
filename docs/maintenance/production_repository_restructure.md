# Production Repository Restructuring, Legacy Removal & Code Consolidation

**Branch:** `restructure/production-cleanup`  
**Checkpoint (pre-deletion):** `9aa8ad47a670b51e2d0c72191c53800c0b44d2c8`  
**Offline suite after restructure:** **611 passed, 5 skipped** (unchanged coverage)  
**Paid LLM calls:** none

This restructuring does **not** change agent behavior, legal thresholds, API contracts, or public-launch blockers.

---

## 1. Original versus final structure

### Before (simplified)

```text
scripts/*.py                         # flat, phase-suffixed runners mixed in
backend/lifepilot/{ops,eval,...}
docs/maintenance/*phase* narratives
data/runs/phase_9d*.md + bulky eval dirs
tests/test_*_9d*.py
```

### After (simplified)

```text
scripts/
  operations/  maintenance/  database/  evaluation/  corpus/
  (+ thin shims for run_api, run_knowledge_maintenance, ask_lifepilot, check_database, import_seed_sources)
backend/lifepilot/
  api/ agent/ rag/ external/ maintenance/ operations/ evaluation/ db/ ...
config/eval/   # benchmarks, held-out baseline, review queue, fixtures
docs/{architecture,ops,maintenance,README}
data/runs/     # operational only: review packets, maintenance, matrices, backup manifest
archive/       # local historical reports + obsolete scripts (gitignored)
```

---

## 2. Architectural decisions

| Decision | Rationale |
| --- | --- |
| Rename `ops` → `operations`, `eval` → `evaluation` | Clearer production naming; small import surface |
| Keep `agent` verification/grounding in place | Avoid high-risk graph/import churn; responsibilities already documented |
| Keep `rag` / `external` / `maintenance` / `db` names | Already clear; mass moves would not improve clarity enough |
| Organize scripts by role, not phase | Discoverability for operators and frontend-era maintainers |
| Archive then delete historical phase reports | Declutter working tree without losing recovery copies |
| Promote held-out X16–X20 first-run baseline into `config/eval/` | Compact release-audit artifact without bulky acceptance workspace |
| Thin compatibility shims for 5 common entry points | Cron/docs muscle memory without duplicating logic |

---

## 3. Files moved or renamed

### Packages

- `backend/lifepilot/ops/` → `backend/lifepilot/operations/`
- `backend/lifepilot/eval/` → `backend/lifepilot/evaluation/`

### Config

- `config/eval/verification_reliability_9d3.json` → `verification_reliability.json`
- `config/eval/performance_cases_9d4.json` → `performance_cases.json`
- Added `held_out_x16_x20_first_run_*.json(l)` under `config/eval/`

### Scripts (representative)

- Phase runners moved under `scripts/evaluation/` with functional names  
  (e.g. `run_performance_optimization_9d4.py` → `run_performance_benchmark.py`)
- Ops/maintenance/database/corpus scripts relocated accordingly
- `run_source_freshness_check.py` → `maintenance/check_source_freshness.py`

### Tests

- `test_api_security_9d5.py` → `test_api_security.py`
- `test_maintenance_lock_9d5.py` → `test_maintenance_lock.py`
- `test_maintenance_reindex_9d6.py` → `test_maintenance_reindex.py`
- `test_performance_optimization_9d4.py` → `test_performance_optimization.py`
- `test_verification_reliability_9d3.py` → `test_verification_reliability.py`

### Compose

- Maintenance entrypoint → `scripts/maintenance/run_knowledge_maintenance.py`

---

## 4. Code consolidated

- Shared eval helpers remain a single package (`lifepilot.evaluation`)
- Operational health/lock/redaction remain a single package (`lifepilot.operations`)
- Duplicate phase-named config keys renamed to functional names
- No second verification engine; no agent fork

---

## 5–6. Code / reports deleted

See [`deletion_manifest.json`](deletion_manifest.json).

Summary:

- Removed active `data/runs/phase_9d*.md` and bulky historical eval directories after archival
- Removed obsolete scripts: grounding audit report writers, one-shot 9D.6 acceptance harness
- Removed historical `docs/maintenance/post_9d2_cleanup_report.md` (archived)

---

## 7. Evaluation tools retained

Under `scripts/evaluation/`:

- Grounded QA, comparative retrieval modes, unified verification, verification reliability (+ fixture builder)
- Performance benchmark / microbench
- API load resilience
- Grounding regression / retrieval eval

Compact fixtures and held-out baseline remain under `config/eval/`.

---

## 8. Configuration cleanup

- Functional rename of reliability/performance fixture files
- Held-out acceptance baseline stored with other eval fixtures
- High-risk review queue and freshness/source catalogs untouched
- Production defaults unchanged

---

## 9. Updated operational commands

```powershell
$env:PYTHONPATH = "backend"
python scripts/operations/run_api.py --host 127.0.0.1 --port 8000
python scripts/maintenance/run_knowledge_maintenance.py --dry-run --limit 10
python scripts/database/check_database.py
python scripts/operations/ask_lifepilot.py --question "…" --jurisdiction GB
pytest -q
docker compose --profile api up -d --build
```

Shims still accept the old top-level script paths for the five most common commands.

---

## 10. Deletion manifest

`docs/maintenance/deletion_manifest.json`

---

## 11. Tests and smoke results

| Check | Result |
| --- | --- |
| Full offline suite | **611 passed, 5 skipped** |
| API / operations / evaluation imports | OK |
| `docker compose config` | OK |
| `run_api.py --help` / maintenance `--help` | OK |
| Paid Anthropic | Not used |

---

## 12. Compatibility and behavior preservation

Unchanged:

- `/v1/answer`, `/health`, `/ready`, `/v1/ops/status`
- `external_mode=auto` + unified verification
- Auth, rate limits, concurrency, SSRF/allowlists
- Maintenance approval rules / migrations / review queues
- Public production remains **not approved**
- `transformers` vulnerability debt remains open

---

## 13. Remaining technical debt

1. Optional deeper package grouping (`knowledge/`, `integrations/`) deferred — current boundaries are clear enough without high-risk moves.
2. Compatibility shims can be removed after operators update cron/docs.
3. `transformers` CVE remediation + embedding regression.
4. Public-launch blockers from Phase 9D.6.
5. Restructure changes on this branch still need an intentional follow-up commit if/when desired.

---

## 14. Final Git status (summary)

- Branch: `restructure/production-cleanup`
- Recoverable checkpoint: `9aa8ad4`
- Restructure edits present on top of checkpoint (packages, scripts, docs, config, tests)
- `.env`, `data/`, `backups/`, `archive/` remain untracked/gitignored as appropriate
- No secrets committed
