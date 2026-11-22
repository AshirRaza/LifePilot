# Phase 10D.4 — Strict Human Verification Enforcement and Governance Integrity

**Date:** 2026-11-22  
**Scope:** Central approval invariant, authorized promotion/demotion, ordinary-ingestion invalidation guard, DB eventless-update triggers, strict/advisory policy, evidence/API integration, legacy reconciliation dry-run  
**Acceptance decision:** **PASS** (with documented limitations → 10D.5 / Phase 14)  
**Live DB:** Unchanged (`alembic_version=20261113_0010`)  
**Production legal-review API:** Remains disabled  

This report does **not** certify any immigration requirement or fabricate legal approvals.

---

## 1. Executive summary

Phase 10D.4 makes human legal verification enforceable end-to-end:

- A LegalRule flagged `human_verified` is **not** authoritative unless a durable approving review event matches exact evidence pins and scope.
- Promotion is an explicit, human-only operation (`promote_legal_rule_from_approval`).
- Read-time enforcement fails closed even when stored flags lag demotion.
- Ordinary crawl/ingest promoting a new current `DocumentVersion` runs the same-transaction review-pin guard.
- PostgreSQL triggers reject eventless review-item state changes and unlinked `human_verified` writes.
- Server-controlled `advisory` vs `strict_verified_only` policy; clients cannot downgrade strict.
- Legacy JSON/`human_verified` flags are operational context only — never grandfathered into approval events.

Full offline suite: **801 passed, 5 skipped** (prior 10D.3 baseline 787).

---

## 2. Governance integrity issue register

| ID | Severity | Defect (pre-10D.4) | Remediation |
| --- | --- | --- | --- |
| I-01 | **Critical** | `review_status=approved` + `verification_status=human_verified` alone established authority | Central `validate_legal_rule_human_authority()` requires linked approved, non-invalidated item + human approval event + pin/scope match |
| I-02 | **Critical** | Ordinary crawl could promote a new current version without review invalidation | `enforce_review_pins_on_new_current()` in `fetcher._persist_version` (same transaction; raise → rollback) |
| I-03 | **High** | Review-item workflow columns updatable without events (Python convention only) | Migration `20261122_0013` trigger `trg_legal_review_items_evented_update` |
| I-04 | **High** | LegalRule could be set `human_verified` without approval linkage | Trigger `trg_legal_rules_human_verified_linkage` |
| I-05 | **High** | Invalidation did not reliably demote LegalRule flags | `demote_if_authority_invalid` on invalidate/reopen/supersede/reject + ingest guard |
| I-06 | **Medium** | Mechanical SUPPORT could be conflated with human legal approval in API | `legal_authority_label` + `legal_verification_policy` on responses; mechanical_match_only false only for curated rules |
| I-07 | **Medium** | Legacy JSON `human_verified` / orphan DB flags | `reconcile_legacy_governance(dry_run=True)` — never invents approval events |
| I-08 | **Medium** | Strict mode not applied across claim verification | `strict_mode_blocks_high_risk_claim` in `verify_material_claims` |
| I-09 | **Low** | Pending-change JSON + truncated test DB ID collision | Authority pending gate matches `previous_version_id` to rule pin; tests isolate pending path |

Confirmed closed in current code (with limitations in §11).

---

## 3. Central approval invariant

**Module:** `backend/lifepilot/governance/approval_invariant.py`  
**API:** `validate_legal_rule_human_authority(session, rule) → ApprovalValidationResult`

Minimum requirements for `ok=True`:

1. `review_status == approved`
2. `verification_status == human_verified`
3. Non-empty `reviewed_by` and `reviewed_at`
4. `document_version_id` set
5. Exactly one non-invalidated `approved` review item with `current_approved_event_id`
6. Approving event: `event_type=approved`, `actor_kind=human_reviewer`, non-empty `actor_id`
7. Item (and event when set) `document_version_id` matches rule pin
8. Evidence fingerprint match when both sides set
9. Pathway / cohort / effective-period match when recorded on the item
10. `reviewed_by` equals approving `actor_id`
11. No unresolved pending source-change whose `previous_version_id` equals the rule pin

Conflicting active approvals → fail closed (`conflicting_active_approvals`).

---

## 4. Promotion / demotion transaction behavior

| Operation | Behavior |
| --- | --- |
| `promote_legal_rule_from_approval` | Requires human `ReviewerIdentity` with `legal_review.decide`; locks item+rule; nonempty rationale; post-condition invariant must pass |
| Service / API-key actors | Rejected (`human_actor_required`) |
| Replay onto new revision | Forbidden — promotion binds rule pin to approved item pin |
| `demote_legal_rule_authority` | Sets `potentially_outdated` / `stale_pending_review`; preserves `reviewed_by`/`reviewed_at` audit; events retained |
| Auto demotion hooks | `append_review_event` on invalidate/reopen/supersede/reject; ingest guard after source-change |

Production HTTP approval routes remain disabled (no IdP).

Synthetic test helper: `ensure_synthetic_human_authority` (isolated fixtures only).

---

## 5. Ordinary-ingestion invalidation integration

After `set_current_version` in `crawl/fetcher._persist_version`, same-transaction:

```text
enforce_review_pins_on_new_current(previous → new)
```

- Shared `classify_source_change_materiality` / `process_source_version_change`
- Metadata-only / equal-hash / no pins → skip
- Classification failure → `materiality_unknown` (fail closed)
- Unexpected errors propagate → fetch transaction rolls back (bytes deleted)
- Idempotent retries: already-invalidated items counted, not re-invalidated

Maintenance material path (10D.3) remains; ingest no longer bypasses pin processing.

---

## 6. Database update enforcement (`20261122_0013`)

Applied on **`lifepilot_test` only**. Live `lifepilot` stays at `0010`.

| Trigger | Effect |
| --- | --- |
| `trg_legal_review_items_evented_update` | Workflow/invalidation/approval-pointer/row_version changes require latest event `to_workflow_state` match |
| `trg_legal_rules_human_verified_linkage` | INSERT/UPDATE to `human_verified` requires linked approved non-invalidated human event + matching version pin |

**DBA recovery:** `DISABLE TRIGGER USER` on affected tables → correct → re-`ENABLE` → record administrative audit. Ordinary app role cannot forge approvals without event rows. Session-variable bypasses were intentionally avoided.

---

## 7. Strict / advisory policy and API behavior

| Policy | Env | Behavior |
| --- | --- | --- |
| `advisory` (default) | `LIFEPILOT_LEGAL_VERIFICATION_POLICY=advisory` | Mechanical support allowed with uncertainty; never labeled human legal approval |
| `strict_verified_only` | `...=strict_verified_only` | High-risk normative claims without `human_verified_rule` evidence → insufficient |

- Client override cannot downgrade enforced strict (`resolve_effective_policy`)
- `AnswerResponse.legal_verification_policy` / `AgentResponse.legal_verification_policy`
- Claim API projection includes `legal_authority_label`
- Rejected claims remain omitted from `claim_verification`

---

## 8. Evidence and retrieval integration

- `query_reviewed_legal_rules` → `legal_rule_authority_block_reason` → invariant
- `evidence_from_legal_rule(..., session=)` refuses unapproved / invariant-failing rules
- EvidenceOrchestrator re-loads rules in-session before curated evidence emission
- `verify_material_claims` applies strict high-risk gate

---

## 9. Legacy reconciliation

`reconcile_legacy_governance(session, dry_run=True)`:

- Reports orphan `human_verified` LegalRules failing the invariant
- Flags JSON queue `human_verified` as non-authoritative
- **Never** creates approval events from legacy statuses
- Optional `dry_run=False` demotes orphan rules only (not used on live DB)

---

## 10. Changed and added files

| Path | Change |
| --- | --- |
| `backend/lifepilot/governance/approval_invariant.py` | **Added** |
| `backend/lifepilot/governance/legal_rule_promotion.py` | **Added** |
| `backend/lifepilot/governance/ingest_version_guard.py` | **Added** |
| `backend/lifepilot/governance/verification_policy.py` | **Added** |
| `backend/lifepilot/governance/legacy_reconciliation.py` | **Added** |
| `backend/lifepilot/governance/synthetic_authority.py` | **Added** (test/support) |
| `backend/lifepilot/governance/__init__.py` | Exports |
| `alembic/versions/20261122_0013_governance_integrity_triggers.py` | **Added** |
| `backend/lifepilot/crawl/fetcher.py` | Ingest pin guard |
| `backend/lifepilot/db/legal_review.py` | Demotion on invalidating events |
| `backend/lifepilot/rag/legal_rules.py` | Invariant block reason |
| `backend/lifepilot/agent/evidence.py` | Session-aware curated evidence |
| `backend/lifepilot/agent/orchestration.py` | Session factory() + reload |
| `backend/lifepilot/agent/verification.py` | Strict policy gate |
| `backend/lifepilot/agent/claim_provenance.py` | Authority label / mechanical_match_only |
| `backend/lifepilot/agent/contracts.py` / `runner.py` | Policy field |
| `backend/lifepilot/api/schemas.py` / `app.py` | Policy on AnswerResponse |
| `tests/test_governance_integrity_10d4.py` | **Added** |
| `tests/conftest.py` | Pending-path isolation |
| Temporal / governance regression fixtures | Synthetic authority seeding |
| `.env.example` | Policy env documented |
| `docs/phase_10d4_governance_integrity_report.md` | **Added** |
| `docs/README.md` | Index |

---

## 11. Test results

| Suite | Result |
| --- | --- |
| Focused 10D.4 + 10B/10C/10D.1–3 regressions | **170 passed** |
| Full offline `tests/` | **801 passed, 5 skipped** |
| Live DB | Unchanged at `0010` |
| Additional LLM calls | None introduced |
| Live mutations / corpus backfills | None |

---

## 12. Security limitations

1. Production reviewer IdP still absent — decision routes stay disabled.
2. App DB role can still INSERT forged `legal_review_events` then update items if it has write privilege; full separation needs dedicated event-writer vs app roles (10D.5 / ops).
3. Trigger checks latest event state match, not a cryptographic proof of human presence.
4. Pending-change registry is file-based; authority gate keys on version pin to avoid ID collision, but is not a distributed lock.
5. Strict mode depends on high-risk detectors + evidence labels — not a substitute for legal review.
6. Migration `0013` is not applied to live DB until a deliberate ops window.

---

## 13. Remaining Phase 10D.5 dependencies

| ID | Item |
| --- | --- |
| D5-01 | Integrated governance acceptance across crawl → review → answer |
| D5-02 | Optional dual DB-role enforcement design + dry-run privilege tests |
| D5-03 | Production IdP wiring (Phase 14) before enabling review API |
| D5-04 | Controlled live migrate `0011`→`0012`→`0013` with backup |
| D5-05 | Operator runbook for trigger-disable recovery and legacy demotion |

---

## 14. Stop confirmation

Phase **10D.4 only** is complete. No 10D.5 integrated acceptance, no 10E legal evaluations, no Phase 14 tenancy/billing/IdP, no live migrations, no production reviewer route enablement.
