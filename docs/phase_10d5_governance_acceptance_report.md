# Phase 10D.5 — Integrated Governance Acceptance, Security Hardening & Targeted Cleanup

**Date:** 2026-11-22  
**Scope:** End-to-end governance acceptance, review-event forgery gate, strict-mode audit, ingestion safety, migration readiness, cleanup  
**Acceptance decision:** **PASS WITH LIMITATIONS**  
**Live DB:** Unchanged (`alembic_version=20261113_0010`)  
**Production legal-review API:** Remains disabled  

This report does **not** certify immigration law, invent legal approvals, or authorize production human-review operations.

---

## 1. Executive summary

Phase 10D.5 closes the critical Phase 10D.4 residual: ordinary SQL could forge `legal_review_events` rows that then satisfied linkage triggers. Migration `20261122_0014` adds an INSERT gate plus `lp_insert_legal_review_event` (SECURITY DEFINER as `lifepilot_gov_writer`). Application writers go only through that path.

Integrated E2E tests cover unreviewed → assign → approve → promote → strict accept/reject → source-change demotion → reapproval, plus direct INSERT forgery rejection.

**Offline suite:** **816 passed, 5 skipped** (prior 10D.4 baseline 801).  
**Live `lifepilot`:** still `0010`. No live migrations, no IdP enablement, no corpus backfills.

---

## 2. Full integration architecture

```text
Official crawl/fetch
  → DocumentVersion (+ set_current)
  → enforce_review_pins_on_new_current (same tx; fail-closed)
  → classify_source_change_materiality
  → process_source_version_change → legal_review_events (gated insert)
  → demote_if_authority_invalid

Unreviewed LegalRule
  → create_pending_review_item (+ submitted_for_review via lp_insert_*)
  → ReviewWorkflowService assign / start / submit_decision
  → append_review_event (gated insert; human actor for decisions)
  → promote_legal_rule_from_approval (human-only; invariant post-check)
  → validate_legal_rule_human_authority (read-time SoT)
  → query_reviewed_legal_rules / evidence_from_legal_rule(session=)
  → EvidenceOrchestrator / verify_material_claims
  → LIFEPILOT_LEGAL_VERIFICATION_POLICY (advisory | strict_verified_only)
  → AgentResponse / AnswerResponse (+ legal_authority_label)
```

Status semantics remain separated:

| Layer | Meaning |
| --- | --- |
| Mechanical SUPPORT | Evidence-backed claim; **not** human legal approval |
| `human_verified_rule` evidence | Passed central approval invariant |
| Review item `approved` | Human decision recorded; LegalRule not auto-promoted |
| LegalRule `human_verified` | Only after explicit promotion + invariant |

---

## 3. Security findings and remediation

| ID | Severity | Finding | Remediation | Status |
| --- | --- | --- | --- | --- |
| S-01 | **Critical** | Direct `INSERT` into `legal_review_events` forged approvals under app login | `0014` INSERT gate + `lp_insert_legal_review_event` | **Fixed** (test DB) |
| S-02 | **High** | Approved item update did not require pointer = latest human approval event | Hardened `enforce_legal_review_item_evented_update` in `0014` | **Fixed** |
| S-03 | **High** | Client-supplied actor / policy fields | API `extra=forbid`; no policy on `AnswerRequest`; reviewer IdP server-side | **Confirmed** |
| S-04 | **Medium** | Superuser/`DISABLE TRIGGER USER` can bypass gates | Documented DBA break-glass; not claimed as human identity | **Accepted limitation** |
| S-05 | **Medium** | Single login may still be DB superuser in local Docker | Gate trigger blocks direct INSERT even for that login; role split recommended for staging | **Limitation → staging** |
| S-06 | **Low** | Trigger checks ≠ human IdP proof | Explicitly documented | **Accepted** |

Forgery attempt covered by `test_direct_sql_event_forgery_blocked`.

---

## 4. Database permission model

| Object | Control |
| --- | --- |
| Role `lifepilot_gov_writer` (NOLOGIN) | Owns `lp_insert_legal_review_event`; granted `INSERT/SELECT` on events |
| Trigger `trg_legal_review_events_insert_gate` | Rejects INSERT unless `current_user = lifepilot_gov_writer` |
| Function `lp_insert_legal_review_event` | SECURITY DEFINER; `EXECUTE` granted to PUBLIC (callable by app) |
| App Python path | `_insert_review_event_row()` only — no ORM direct event INSERT |
| Event UPDATE/DELETE | Still forbidden (`0012` append-only triggers) |

**Honest DBA limitation:** a superuser can disable triggers and rewrite history. That is recovery, not product authentication.

Optional future: non-superuser app login + `LIFEPILOT_GOVERNANCE_DATABASE_URL` (documented in `.env.example`).

---

## 5. End-to-end scenario results

`tests/test_governance_acceptance_10d5.py` — **15 passed**, including:

1. Unreviewed → pending → assign → start → approve → promote → strict accept  
2. Strict reject of mechanical high-risk evidence  
3. Caller cannot downgrade strict / `AnswerRequest` forbids policy field  
4. Cohort / document-version / rule-revision mismatches fail invariant  
5. Source-change invalidation demotes; stale lookup empty; reapproval on new pin  
6. Reassessment queue + freshness alert composition  
7. Ingest failure injection rolls back authority loss  
8. Direct SQL event forgery blocked  
9. Gated writer path works for legitimate creates  
10. Legacy JSON non-promotion  
11. Legal-review API disabled by default  
12. Advisory retains mechanical ≠ human label  
13. Rejected claims omitted from API projection  
14. Invariant overhead bound  
15. Orchestrator curated-rule path  

---

## 6. Ingestion / invalidation reliability

| Scenario | Result |
| --- | --- |
| New content version via fetcher | Guard runs same-tx after `set_current_version` |
| Equal-hash / metadata-only | Skip invalidation |
| Potentially material / unknown | Invalidate high-risk pins; demote rules |
| Failed `process_source_version_change` | Exception → nested rollback; authority retained |
| Idempotent retry | Already-invalidated counted; no duplicate authority restore |
| Maintenance demote-to-previous | Intentionally restores prior current; pins already handled on promote path |

**Residual:** direct `is_current=True` writes in tests/helpers bypass fetcher (not production crawl). Ordinary crawl path is covered.

---

## 7. Strict-mode enforcement findings

| Path | Bypass? |
| --- | --- |
| Internal RAG passages | No — high-risk blocked without human rule evidence |
| External connectors | Same verification gate on claims |
| Legacy `human_verified` flags | Fail invariant / DB trigger without linkage |
| Invalidated rules | Demoted + excluded from `query_reviewed_legal_rules` |
| Historical / cohort / pathway | Existing 10B/10C gates retained |
| API policy downgrade | Impossible via `AnswerRequest` (`extra=forbid`) |
| Mechanical SUPPORT labeled human | `legal_authority_label` distinguishes |

No additional confirmed critical strict-mode bypasses in tested scope.

---

## 8. Cleanup summary

| Action | Notes |
| --- | --- |
| Wired all event writes through gated insert | `legal_review.py` |
| Documented optional governance DB URL | `.env.example` |
| Operator runbook added | `docs/ops/governance_migration_runbook.md` |
| Phase 10D.1–10D.4 reports | **Retain** as acceptance history (do not delete) |
| Archive plan | Intermediate disposable debug scripts only; keep final phase reports under `docs/` |
| No broad renames / no behavior rewrites of accepted phases | Beyond forgery gate + revision metadata pin |

---

## 9. Migration readiness assessment

| Check | Result |
| --- | --- |
| Chain `0010→0011→0012→0013→0014` | Linear; applied on `lifepilot_test` |
| Live `lifepilot` | Remains `0010` |
| Triggers / function / role | Present on test DB |
| App startup / suite | Green |
| Rollback procedure | Documented (prefer dump restore) |
| Runbook | `docs/ops/governance_migration_runbook.md` |

**Do not** migrate live without staging sign-off.

---

## 10. Performance measurements

Representative local measurements against `lifepilot_test` (synthetic single rule):

| Operation | N | Total | Average |
| --- | --- | --- | --- |
| `validate_legal_rule_human_authority` | 100 | 1391 ms | **13.9 ms** |
| `query_reviewed_legal_rules` | 50 | 1026 ms | **20.5 ms** |
| Full offline pytest | — | **97.5 s** | 816 pass / 5 skip |

Overhead is acceptable for governance-critical reads. No new default LLM calls.

---

## 11. Full regression results

| Suite | Result |
| --- | --- |
| 10D.5 acceptance | 15 passed |
| 10D.1–10D.4 governance | passed (included in full) |
| 10B temporal / 10C evidence | passed (included in full) |
| Full `tests/` | **816 passed, 5 skipped** |
| Live DB stamp | `20261113_0010` |

---

## 12. Outstanding issue register

| ID | Milestone | Item |
| --- | --- | --- |
| L-01 | Staging ops | Apply `0011–0014` on staging clone; verify dump/restore |
| L-02 | Staging ops | Non-superuser app DB login + revoke unnecessary grants |
| L-03 | Phase 14 | Production human IdP before enabling review API |
| L-04 | Ops | Schedule reassessment / maintenance runners |
| L-05 | Ops | Governance audit log retention & DR drills |
| L-06 | Product | Strict-mode default policy decision per environment |
| L-07 | Future | Dual connection URL for dedicated governance writer (optional) |

---

## 13. Production IdP and deployment dependencies

### A — Required for engineering acceptance (this phase)

- Gated event insert + E2E + regressions — **done**
- Live DB untouched — **done**
- Reviewer API remains disabled — **done**

### B — Required before private operational staging

- Staging migrate `0011→0014` per runbook  
- Backup/restore proof on staging clone  
- Explicit `LIFEPILOT_LEGAL_VERIFICATION_POLICY`  
- Non-production `test_fixture` auth only if human workflow demos needed  

### C — Required before production legal-review use

- Real human IdP (not test_fixture)  
- Network-restricted reviewer API enablement  
- App DB role least-privilege hardening beyond local superuser Docker  
- Operator on-call for trigger break-glass  

### D — Future commercial / Phase 14

- Tenancy, billing, multi-org reviewer directories  
- Dedicated governance writer credentials rotation  

---

## 14. Acceptance decision

**PASS WITH LIMITATIONS**

Critical forgery path under ordinary application DML is closed on the migrated test schema; integrated lifecycle and strict-mode controls hold in offline tests. Limitations are operational (live migrate, IdP, non-superuser role split), not unresolved engineering bypasses in the tested scope.

---

## 15. Explicit blockers before Phase 10E

1. Do **not** treat offline PASS as independent legal accuracy certification (10E scope).  
2. Do **not** enable production reviewer routes without IdP (Phase 14 / C above).  
3. Do **not** migrate live `lifepilot` without staging runbook completion (L-01).  
4. Phase 10E may begin only after product accepts that remaining items are ops/IdP, not governance-engine defects.

---

## 16. Changed / added files (10D.5)

| Path | Change |
| --- | --- |
| `alembic/versions/20261122_0014_governance_event_insert_gate.py` | **Added** |
| `backend/lifepilot/db/legal_review.py` | Gated event insert helper |
| `backend/lifepilot/governance/approval_invariant.py` | Rule-revision pin check |
| `backend/lifepilot/governance/review_workflow.py` | Approval metadata revision |
| `backend/lifepilot/governance/synthetic_authority.py` | Revision metadata |
| `tests/test_governance_acceptance_10d5.py` | **Added** |
| `docs/ops/governance_migration_runbook.md` | **Added** |
| `docs/phase_10d5_governance_acceptance_report.md` | **Added** |
| `docs/README.md` | Index |
| `.env.example` | Governance URL note |

---

## 17. Stop confirmation

Phase **10D.5 only** is complete. No Phase 10E benchmarking, no Phase 14 IdP/tenancy, no live migrations, no corpus backfills, no production reviewer API activation, no scheduled background jobs enabled.
