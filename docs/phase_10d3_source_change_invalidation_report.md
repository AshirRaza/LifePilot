# Phase 10D.3 — Automatic Source-Change Invalidation, Reassessment & Maintenance Alerts

**Date:** 2026-11-22  
**Scope:** Source-version → review invalidation, materiality policy, reassessment queues, alert/freshness fixes, LegalRule stale-authority bridge, dry-run runner  
**Acceptance decision:** **PASS** (with documented 10D.4 dependencies)  
**Live DB:** Unchanged (`alembic_version=20261113_0010`)  
**Production legal-review API:** Remains disabled  

This report does **not** certify that any website update is a change in immigration law.

---

## 1. Executive summary

Phase 10D.3 connects official-source version lifecycle to persistent legal-review governance:

- Deterministic **materiality classification** (webpage update ≠ legal change)
- Idempotent **`source_version_changed`** invalidation for review items pinned to the previous `DocumentVersion`
- Hook from knowledge-maintenance material/uncertain path
- **Reassessment queue** (urgent / overdue / due / provenance)
- Fixed legacy **freshness alert status mismatch**
- Conservative **read-time gate** so invalidated linked rules cannot pass `query_reviewed_legal_rules`
- CLI runner: `scripts/maintenance/run_legal_review_reassessment.py` (**dry-run default**)

Full offline suite: **787 passed, 5 skipped** (prior 10D.2 baseline 778).

---

## 2. Monitoring architecture audit

| Layer | Role |
| --- | --- |
| `crawl/fetcher.py` | New `DocumentVersion` when content SHA differs; unchanged rechecks update HTTP metadata only |
| `maintenance/change_classify.py` | Text heuristic: `unchanged` / `editorial` / `potentially_material` / `uncertain` / `fetch_failed` |
| `extract/compare.py` | Structured comparison: `unchanged` / `changed` / `unavailable` |
| `maintenance/pipeline.py` | Due-document recheck; demotes material/uncertain (keeps previous `is_current`); now invalidates review pins |
| `pending_material_changes.json` | Operator/agent pending registry |
| `legal_review_*` | Append-only governance ledger |

Two taxonomies remain (heuristic vs structured). 10D.3 maps both into `SourceChangeMateriality` rather than inventing a third crawl system.

---

## 3. Confirmed legacy defects and remediations

| ID | Severity | Defect | Fix |
| --- | --- | --- | --- |
| A-01 | **High** | `freshness_alerts.json` filtered `change_class.startswith("material")` and `status==queued_for_review`, missing `potentially_material` / `pending_review` | `build_freshness_alerts()` + updated `run_knowledge_maintenance.py` |
| A-02 | **Medium** | `pending_uncertainty_reasons` treated `potentially_material` only as status | Also match `change_class` |
| A-03 | **High (gap)** | No auto `source_version_changed` on material rechecks | `process_source_version_change` + pipeline hook |
| A-04 | **Medium** | Human-verified LegalRule could remain authoritative after review invalidation | `legal_rule_authority_block_reason` in `query_reviewed_legal_rules` |
| A-05 | **Low** | Freshness/review JSON `@lru_cache` could hide updates | `reset_freshness_and_review_caches()` after execute runner |

---

## 4. Source-change classification policy

| Label | Typical signals | Invalidates high-risk approvals? |
| --- | --- | --- |
| `no_meaningful_content_change` | Equal content hash / unchanged text | No |
| `metadata_only` | ETag/Last-Modified only | No |
| `nonmaterial_content_change` | Editorial / cookie-chrome noise | No |
| `potentially_material` | Salary/eligibility/date heuristics | **Yes** (fail-closed) |
| `confirmed_material` | Heuristic material + structured `changed` | **Yes** |
| `materiality_unknown` | Uncertain / fetch ambiguity | **Yes** for high/medium |

**Rationale:** Operators must reassess when materiality is potential or unknown; approvals never auto-inherit onto a newer version. Unrelated documents/jurisdictions are untouched (pin by `document_version_id` only).

---

## 5. Invalidation behavior and transactions

1. Identify items with `document_version_id == previous_version_id`.
2. Skip superseded / already-recorded same `new_version_id`.
3. Apply policy via `should_invalidate_review_item`.
4. `append_review_event(event_type="source_version_changed", actor=service:source_change_monitor)`.
5. Clears `current_approved_event_id`; preserves prior `approved` events (append-only).
6. Dry-run: zero writes.

`ALLOWED_TRANSITIONS` expanded so active states may move to `invalidated` via source-change (fail-closed).

---

## 6. LegalRule stale-approval safeguards

Until 10D.4 promotion sync exists:

- `legal_rule_authority_block_reason()` excludes `human_verified` rules when a linked review item is invalidated.
- Optionally blocks when pending material registry references the rule’s `document_id`.
- **Does not** rewrite `LegalRule` verification columns.
- Absence of review linkage for legacy verified rows remains a **10D.4** hardening item; pending-document gate covers many corpus cases.

---

## 7. Reassessment scheduling

`build_reassessment_queue(as_of=UTC)` emits one entry per item with highest urgency:

| Urgency | Trigger |
| --- | --- |
| `urgent` | High-risk invalidated |
| `overdue` | `reassessment_at` / `review_deadline_at` &lt; as_of |
| `due` | Deadline within 7-day approaching window |
| `provenance` | Missing evidence pin |

Deadlines are never extended. No time-based auto-approval.

---

## 8. Maintenance alert integration

`build_freshness_alerts` populates:

- `pending_material_changes` (deduped)
- `overdue_sources` / fetch failures
- `high_risk_review_invalidations`
- `overdue_reassessments`
- `missing_provenance`

Notes explicitly state potential changes are **not** conclusive legal findings. Readiness (`operations/health.py`) continues to consume `freshness_alerts.json`.

---

## 9. Runner CLI

```text
python scripts/maintenance/run_legal_review_reassessment.py --dry-run
python scripts/maintenance/run_legal_review_reassessment.py --execute --limit 50 --json
python scripts/maintenance/run_legal_review_reassessment.py --execute --jurisdiction-id N --pathway-id PATH
```

Flags (from source): `--dry-run` (default), `--execute`, `--limit`, `--jurisdiction-id`, `--pathway-id`, `--json`, `--out`.

Does not install schedulers. Do not run `--execute` against live `lifepilot`.

Knowledge maintenance also remains:  
`run_knowledge_maintenance.py --dry-run | --execute --limit N --jurisdiction CODE`.

---

## 10. Changed / added files

| Path | Change |
| --- | --- |
| `backend/lifepilot/governance/source_change.py` | **Added** |
| `backend/lifepilot/governance/reassessment.py` | **Added** |
| `backend/lifepilot/governance/maintenance_alerts.py` | **Added** |
| `backend/lifepilot/governance/__init__.py` | Exports |
| `backend/lifepilot/db/legal_review.py` | Transition graph for source invalidation |
| `backend/lifepilot/maintenance/pipeline.py` | Invalidation hook |
| `backend/lifepilot/maintenance/pending_changes.py` | change_class-aware uncertainty |
| `backend/lifepilot/rag/legal_rules.py` | Authority block bridge |
| `backend/lifepilot/agent/retrieval_policy.py` | Cache reset helper |
| `scripts/maintenance/run_knowledge_maintenance.py` | Alert filter fix |
| `scripts/maintenance/run_legal_review_reassessment.py` | **Added** runner |
| `tests/test_source_change_invalidation.py` | **Added** |
| `docs/phase_10d3_source_change_invalidation_report.md` | **Added** |
| `docs/README.md` | Index |

No new Alembic migration.

---

## 11. Test results

| Suite | Result |
| --- | --- |
| Focused 10D.3 + maintenance + 10D.1/2 | **46 passed** |
| Full offline `tests/` | **787 passed, 5 skipped** |
| Live DB | Unchanged at `0010` |

---

## 12. Remaining limitations → 10D.4–10D.5

| ID | Phase | Item |
| --- | --- | --- |
| D4-01 | 10D.4 | Synchronized LegalRule demotion / promotion with approved events |
| D4-02 | 10D.4 | Require review linkage for all human_verified rules |
| D4-03 | 10D.4 | Eventless item-update DB enforcement |
| D4-04 | 10D.4 | Production reviewer IdP + enable gated API |
| D3-LIM-1 | — | Ingest path still can promote versions without maintenance demote (ops process) |
| D3-LIM-2 | — | Classifier is heuristic — not legal proof of regulatory change |
| D5-01 | 10D.5 | Integrated governance acceptance |

---

## 13. Stop confirmation

Phase **10D.3 only** is complete. No 10D.4 promotion framework, no 10D.5 acceptance, no 10E, no Phase 14 auth, no live migrations, no scheduled background jobs.
