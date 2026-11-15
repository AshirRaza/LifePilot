# Post-9D.2 Repository Cleanup Report

**Date:** 2026-11-15  
**Scope:** Conservative organization and maintainability cleanup after Phase 9D.2  
**Constraint:** No intentional behavior changes; no schema/source-approval changes; no paid eval re-runs

---

## 1. Original organization and problems identified

LifePilot’s agent stack already followed a workable flow:

**registry/ingest → retrieval (orchestration + policy) → generation → grounding → legal verification → response**

Main cleanup issues (not architectural gaps):

| Issue | Notes |
|---|---|
| Private grounding helpers used by verification | `_overlap_ok` / `_numeric_literals_supported` imported across modules |
| Duplicated numeric normalization | Same strip logic in `grounding.py` and `pathway_applicability.py` |
| Duplicated eval cost accounting | Near-identical `_estimate_cost` in 9D.1 and 9D.2 scripts |
| Undocumented verification env knobs | `LIFEPILOT_VERIFICATION_*` missing from `.env.example` |
| Stale README metrics / thin script index | Test count outdated; no script role map |
| Transient caches | Local `__pycache__` / `.pytest_cache` noise |

**Intentionally not “fixed” as duplication:** grounding then verification re-check overlap/numeric/pathway. That defense-in-depth is the unified verification gate for production and evaluation.

---

## 2. Files moved, renamed, removed, or simplified

| Action | Path |
|---|---|
| Added | `backend/lifepilot/eval/accounting.py` — shared cost helper |
| Added | `scripts/README.md` — operational vs evaluation script index |
| Added | `docs/maintenance/post_9d2_cleanup_report.md` (this file) |
| Simplified | `grounding.py` — public `claim_*` helpers; shared numeric normalize |
| Simplified | `pathway_applicability.py` — public `normalize_numeric_literal` |
| Updated | eval scripts to call shared accounting |
| Updated | `.env.example`, `README.md`, `development_history.md`, agent package docstring |
| Cleared | local `__pycache__` / `.pytest_cache` (regenerable) |

**No scripts deleted.** All CLIs still have operational or evaluation purpose after reference checks.

**No historical 9C/9D artifacts moved or deleted.**

9D.2 deliverables that remain untracked until commit (feature work, not cleanup-only):  
`verification.py`, `corroboration.py`, `run_unified_verification_eval.py`, `test_unified_legal_verification.py`, plus related agent/graph edits from 9D.2.

---

## 3. Duplicate functionality consolidated

| Before | After |
|---|---|
| Private grounding overlap/numeric APIs | `claim_passage_overlap_ok`, `claim_numeric_literals_supported` |
| Two `_normalize_numeric` copies | `normalize_numeric_literal` in pathway; grounding reuses it |
| Two eval `_estimate_cost` bodies | `lifepilot.eval.accounting.estimate_usage_cost_usd` |

**Preserved (not merged away):**

- Grounding inside generation **and** verification re-checks (single verifier implementation; dual application is intentional)
- Deprecated `LIFEPILOT_AGENT_EXTERNAL_FALLBACK` flag (compat / existing tests)
- Evaluation-only `external_only` adapter (not promoted to production)

---

## 4. Configuration changes

| Setting | Change |
|---|---|
| Production defaults | **Unchanged** (`external_mode=auto`, verification enabled, max external corroboration `0`) |
| `.env.example` | Documented `LIFEPILOT_VERIFICATION_ENABLED` and `LIFEPILOT_VERIFICATION_MAX_EXTERNAL`; clarified deprecated fallback |
| `AgentConfig` comments | Clarified deprecated fallback vs active verification knobs |

---

## 5. Evaluation and operational scripts

- Added `scripts/README.md` separating operational, evaluation, and coverage CLIs
- Comparative + unified verification evals now share cost accounting
- Confirmed eval continues to call `run_agent` (same verification node as production)
- Held-out X16–X20 and immutable `agent_qa_benchmark_v1` untouched

---

## 6. Documentation updated

- `README.md` — pipeline note, test count, eval commands, `eval/` + `maintenance/` in tree
- `docs/maintenance/development_history.md` — keep/regenerate table + config cheat sheet
- `.env.example` — verification settings
- `backend/lifepilot/agent/__init__.py` — subsystem map docstring
- `backend/lifepilot/eval/__init__.py` — note that verification is not reimplemented for eval

---

## 7. Before / after test counts

| | Passed | Skipped |
|---|---:|---:|
| Before cleanup | 575 | 5 |
| After cleanup | 575 | 5 |

No paid LLM evaluation re-run (not required for cleanup).

---

## 8. Preserved behavior (confirmed)

- Automatic hybrid retrieval remains the production default
- Unified verification remains enabled in the main agent graph
- F07/X05/X06 pathway locality + URL-authoritative signals retained
- Numerical / citation checks and free-text rebuild protections retained
- Source allowlists / maintenance / review queues unchanged
- Evaluation uses production verification (no parallel engine)
- No additional default paid LLM or external corroboration calls

---

## 9. Remaining technical debt (deferred)

| Item | Why deferred |
|---|---|
| Move grounding call out of `generation.py` into the verification node only | Behavior-preserving but larger refactor; current defense-in-depth is explicit |
| Split `scripts/` into physical subdirectories | Would churn many docs/CI references for little gain; README index is enough for now |
| Remove deprecated `external_fallback_on_insufficient_internal` | Still referenced by tests; needs a dedicated deprecation phase |
| Enable `LIFEPILOT_VERIFICATION_MAX_EXTERNAL=1` in staging | Behavioral/latency trade-off; not a cleanup change |
| Human-fill 9D.1 reference evidence packets | Legal/process work, not code cleanup |

---

## 10. Final Git status (after cleanup)

Tracked modifications include 9D.2 feature files plus cleanup edits. Untracked new modules:

```
?? backend/lifepilot/agent/corroboration.py
?? backend/lifepilot/agent/verification.py
?? backend/lifepilot/eval/accounting.py
?? scripts/README.md
?? scripts/run_unified_verification_eval.py
?? tests/test_unified_legal_verification.py
?? docs/maintenance/post_9d2_cleanup_report.md
```

Plus modified agent/eval/docs/config/README files from 9D.2 + cleanup.

`data/` run artifacts remain gitignored and were not deleted.
