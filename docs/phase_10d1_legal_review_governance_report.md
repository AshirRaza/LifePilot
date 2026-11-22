# Phase 10D.1 — Persistent Legal Review Governance Data Model

**Date:** 2026-11-22  
**Scope:** Database architecture, persistence primitives, migration `20261122_0012`, automated tests  
**Acceptance decision:** **PASS** (engineering foundation only)  
**Live DB:** Unchanged (`alembic_version=20261113_0010`; `document_chunks=9883`; no `legal_review_*` tables)  
**Out of scope (explicit stop):** 10D.2 interactive workflows, 10D.3 auto-invalidation/scheduling, 10D.4 verification/API enforcement, 10D.5 acceptance, 10E benchmarks, commercial auth/tenancy

This report does **not** certify any immigration requirement as human-verified.

---

## 1. Executive summary

Phase 10D.1 adds a fail-closed, append-only foundation for human legal review:

- Tables `legal_review_items` and `legal_review_events`
- Repository helpers in `lifepilot.db.legal_review` (create/get/list/append/history)
- Alembic revision `20261122_0012` revising `20261118_0011`
- UPDATE/DELETE triggers on events; optimistic `row_version` concurrency
- Evidence pinning via `document_version_id` and/or `evidence_fingerprint`
- Actor kinds distinguishing `human_reviewer` / `service` / `system`
- **No** promotion of `LegalRule.verification_status` to `human_verified`

Full offline suite: **766 passed, 5 skipped** (prior 10C.5 baseline 753; +13 governance tests).

---

## 2. Existing governance inventory

| Location | What it stores | Authoritative? | Notes for 10D |
| --- | --- | --- | --- |
| `legal_rules.review_status` / `verification_status` / `reviewed_by` / `reviewed_at` | Rule-level review + verification | **Yes for LegalRule verification** (Phase 10B) | Remains SoT for whether a rule is `human_verified`. Check `ck_legal_rules_human_verified_requires_audit` retained. |
| `sources.review_status` | Source allowlist approval | Separate (crawl/registry) | Not legal-claim approval; do not conflate. |
| `config/eval/high_risk_legal_review_queue.json` | High-risk claim queue (status, reviewer null) | **Non-authoritative** | Bridge via `subject_type=high_risk_queue_item` later; keep functional in 10D.1. |
| Coverage CSV/JSON review packets (`phase_6e1_review_queue.csv`, readiness decisions) | Pathway/requirement confirm decisions | Non-authoritative for LegalRule | Explicitly “COMPLETE ≠ human_verified”. |
| Maintenance review packets | `human_verified: false` by construction | Non-authoritative | Stay as operational artifacts. |
| API `AuthPrincipal` (`key_N_<digest>`) | Service API key principal | Not a human reviewer | Insufficient for legal approval identity. |
| Change-detection / freshness / alerts | Corpus freshness signals | Not approval records | 10D.3 will emit `source_version_changed` / invalidation events. |

### Gaps addressed by 10D.1

1. **Duplicated review state** — JSON/CSV queues + LegalRule columns with no shared work-item ledger.  
2. **Missing durable history** — no append-only decision trail.  
3. **Weak reviewer authorization** — API keys are service principals; no human actor model.  
4. **Approvals without pinned versions** — JSON queue lacks `document_version_id`.  
5. **Source changes do not invalidate approvals** — no event/invalidation state (hooks deferred to 10D.3).  
6. **Legacy to migrate** — JSON/CSV remain writable for ops; DB is the future SoT for legal approvals (reconciliation in 10D.2–10D.4).

### Design choice

Introduce **two cohesive tables** rather than parallel approval systems. `LegalRule` verification fields stay authoritative for rule verification; `legal_review_*` records workflow and audit. Until 10D.4, an `approved` review item **must not** silently set `verification_status=human_verified`.

---

## 3. Modified and added files

| Path | Change |
| --- | --- |
| `alembic/versions/20261122_0012_legal_review_governance.py` | **Added** — upgrade/downgrade |
| `backend/lifepilot/db/models.py` | **Added** enums + `LegalReviewItem` / `LegalReviewEvent` |
| `backend/lifepilot/db/legal_review.py` | **Added** — persistence primitives |
| `backend/lifepilot/db/__init__.py` | Export new models |
| `backend/lifepilot/db/isolation.py` | Clear order + trigger disable for tests; re-seed embedding model |
| `tests/test_legal_review_governance.py` | **Added** — focused governance + migration + live-DB guard |
| `docs/phase_10d1_legal_review_governance_report.md` | **Added** — this report |
| `docs/README.md` | Index entry |

`20261118_0011` was **not** modified.

---

## 4. Data model and migration design

### `legal_review_items`

Stable work item: `public_id` (UUID), `subject_type` (`legal_rule` \| `claim_evidence` \| `high_risk_queue_item`), optional `legal_rule_id` / `subject_ref`, jurisdiction/pathway/category, `risk_class`, `workflow_state` (default **`pending`**), `review_reason`, `priority`, source/document/`document_version_id`, `evidence_fingerprint`, reviewed effective/cohort scope, assignment fields, deadline/reassessment, `invalidated` + reason, `current_approved_event_id`, `row_version`, UTC timestamps.

Key CHECKs:

- Evidence pin required (`document_version_id` OR non-empty fingerprint)
- `legal_rule` subjects require both `legal_rule_id` and `document_version_id`
- Controlled enums for state/risk/priority/actor kind

### `legal_review_events`

Append-only history: `event_type`, `from_workflow_state` / `to_workflow_state`, `occurred_at`, `actor_id` + `actor_kind`, rationale, evidence pin fields, `metadata` JSONB object.

Triggers `trg_legal_review_events_no_update` / `_no_delete` call `prevent_legal_review_event_mutation()`.

### Migration

- Revision: `20261122_0012` → revises `20261118_0011`
- Additive only; no LegalRule/DocumentVersion data mutation
- Tested upgrade to head and downgrade to `0011` on `lifepilot_test`
- Live `lifepilot` **not** migrated

---

## 5. Workflow invariants

| Invariant | Enforcement |
| --- | --- |
| New items start `pending` / not human-verified | Server default + create helper; LegalRule untouched |
| Existence / assignment / automation ≠ verified | Documented; no promotion code path in 10D.1 |
| Prior version approval not inherited | `source_version_changed` → `invalidated`; clears `current_approved_event_id` |
| Allowed transitions | `ALLOWED_TRANSITIONS` in `legal_review.py` |
| Human decisions require human actor | `HUMAN_DECISION_EVENTS` → `actor_kind=human_reviewer` |
| Concurrent updates | `SELECT … FOR UPDATE` + `expected_row_version` |
| LegalRule audit constraint | Unchanged `ck_legal_rules_human_verified_requires_audit` |

**Intended relationship (later phases):** only an authorized transition may set `LegalRule.verification_status=human_verified` **and** must require matching pinned `document_version_id`, `review_status=approved`, `reviewed_by`/`reviewed_at`, and a durable approving event. 10D.1 records approval on the **item** only.

---

## 6. Reviewer identity assumptions

| Kind | Meaning | May approve? |
| --- | --- | --- |
| `human_reviewer` | Authenticated human (future server-side auth) | Yes (when authorized) |
| `service` | Pipelines / maintenance | No for decision events |
| `system` | Migrations / recovery tooling | Restricted |

Compatibility design (no Phase 14 rewrite):

- Persist opaque `actor_id` strings issued by **server-side** auth later (e.g. `reviewer:<stable_id>`).
- Do **not** trust client-supplied user IDs as authorization.
- Current API key principals (`key_N_…`) are **service**, not human reviewers.
- No anonymous approvals (`actor_id` non-empty CHECK).

---

## 7. Evidence version pinning

- Legal-rule reviews **require** `document_version_id` matching `LegalRule.document_version_id` when set.
- Claim/queue subjects may pin via fingerprint alone.
- Events copy the examined pin at append time.
- A newer `DocumentVersion` does not inherit prior item approval; emit `source_version_changed` (full automation in 10D.3).

---

## 8. Append-only guarantees and limitations

**Guarantees (ordinary app role):**

- INSERT-only application path for events
- BEFORE UPDATE/DELETE triggers raise
- Item state changes only through `append_review_event` under row lock

**Limitations (honest):**

- Table owners / superusers can `DISABLE TRIGGER` or alter rows
- Application could theoretically UPDATE `legal_review_items` without an event (10D.4 should tighten)
- Soft pointer `current_approved_event_id` can be cleared on invalidation (event rows remain)

### Administrative recovery procedure

1. Record incident ticket + reason.  
2. As DB owner on a maintenance window: `ALTER TABLE legal_review_events DISABLE TRIGGER USER`.  
3. Apply the minimal corrective SQL; prefer inserting `administrative_correction` via app API when available.  
4. `ENABLE TRIGGER USER`; verify counts and latest event chain.  
5. Never rewrite history silently from application code.

---

## 9. Legacy compatibility strategy (10D.2–10D.4)

| Phase | Action |
| --- | --- |
| **10D.1 (now)** | JSON/CSV queues remain operational; DB is additive and unused by product APIs |
| **10D.2** | Interactive workflow writes **only** to `legal_review_*`; import/bridge high-risk JSON as `high_risk_queue_item` subjects (read-migrate, stop treating JSON status as approval) |
| **10D.3** | Source-change detection appends invalidation events; maintenance alerts reference item IDs |
| **10D.4** | Verification/API refuse `human_verified` without matching approved, non-invalidated review event + pin; LegalRule promotion becomes explicit gated operation |

**Reconciliation rule:** until 10D.4 cutover, LegalRule `verification_status` remains the only field that means “human-verified rule.” JSON `human_verified` / `requires_human_review` labels are queue-ops only.

---

## 10. Test results

| Suite | Result |
| --- | --- |
| `tests/test_legal_review_governance.py` | **13 passed** |
| Full offline `tests/` | **766 passed, 5 skipped** |
| Live `lifepilot` | `20261113_0010`, 9883 chunks, no `legal_review_items` |

Covered acceptance themes: default pending, FK/linkage, version pin, missing provenance rejection, create, append, immutability, invalid transitions, missing actor, service vs human, concurrency, approval non-promotion, superseded/version-change invalidation, LegalRule audit constraint, upgrade/downgrade + corpus preservation, live DB unchanged, regressions via full suite.

---

## 11. Remaining work → 10D.2–10D.5

| ID | Phase | Item |
| --- | --- | --- |
| D2-01 | 10D.2 | Interactive assign/start/approve/reject UX + server authorization |
| D2-02 | 10D.2 | Import/bridge JSON high-risk queue → review items |
| D3-01 | 10D.3 | Automatic `source_version_changed` on new DocumentVersion |
| D3-02 | 10D.3 | Reassessment scheduling / maintenance alerts |
| D4-01 | 10D.4 | Gate LegalRule → `human_verified` on review event + pin |
| D4-02 | 10D.4 | API enforcement; refuse client-trusted reviewer IDs |
| D4-03 | 10D.4 | Prevent silent item state updates without events |
| D5-01 | 10D.5 | Governance acceptance pack |

---

## 12. Stop confirmation

Phase **10D.1 only** is complete. No 10D.2+ workflows, no live migration, no fabricated real legal approvals, no commercial account infrastructure.
