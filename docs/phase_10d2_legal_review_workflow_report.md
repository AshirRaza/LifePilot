# Phase 10D.2 — Authorized Human Review Workflow and Legacy Queue Integration

**Date:** 2026-11-22  
**Scope:** Review workflow service, fail-closed reviewer authorization, gated HTTP API, high-risk JSON queue bridge, tests  
**Acceptance decision:** **PASS** (with documented auth integration dependency)  
**Live DB:** Unchanged (`alembic_version=20261113_0010`; no `legal_review_*` tables)  
**Out of scope:** 10D.3 auto-invalidation, 10D.4 LegalRule promotion/enforcement, 10D.5 acceptance, 10E, Phase 14 commercial auth

This report does **not** certify any immigration requirement as human-verified.

---

## 1. Executive summary

Phase 10D.2 adds an authorized workflow layer on top of the 10D.1 persistence model:

- `ReviewWorkflowService` — list/filter, assign, start, decide, reopen (all via `append_review_event`)
- Fail-closed `ReviewerAuthProvider` with permissions `legal_review.read|assign|decide|admin`
- HTTP routes under `/v1/legal-review/*` **disabled by default**; production keeps them off
- Test-only human identity fixture (`auth_mode=test_fixture`) for isolated verification
- Idempotent high-risk JSON importer (dry-run default; quarantines missing provenance)
- Approved review items **do not** mutate `LegalRule` verification fields

Full offline suite: **778 passed, 5 skipped** (prior 10D.1 baseline 766; +12 net new tests in this phase’s suite contribution).

---

## 2. Changed and added files

| Path | Change |
| --- | --- |
| `backend/lifepilot/governance/__init__.py` | **Added** package exports |
| `backend/lifepilot/governance/reviewer_auth.py` | **Added** identity/permissions/providers |
| `backend/lifepilot/governance/review_workflow.py` | **Added** workflow service |
| `backend/lifepilot/governance/high_risk_queue_import.py` | **Added** JSON→DB bridge |
| `backend/lifepilot/db/legal_review.py` | Extended list filters; `assigned_to`; reopen clears invalidation |
| `backend/lifepilot/api/deps.py` | **Added** `get_db` |
| `backend/lifepilot/api/legal_review_routes.py` | **Added** gated review routes |
| `backend/lifepilot/api/schemas.py` | Legal review request/response models |
| `backend/lifepilot/api/settings.py` | `legal_review_api_enabled`, auth mode |
| `backend/lifepilot/api/app.py` | Register router; reset auth cache |
| `.env.example` | Document legal-review env vars |
| `tests/test_legal_review_workflow.py` | **Added** focused 10D.2 tests |
| `docs/phase_10d2_legal_review_workflow_report.md` | **Added** this report |
| `docs/README.md` | Index entry |

No new Alembic migration required.

---

## 3. Review workflow service architecture

```
HTTP /v1/legal-review/*
  → AuthPrincipal (service API key transport)
  → ReviewerAuthProvider.resolve_human_reviewer (headers)
  → permission check
  → ReviewWorkflowService
       → list_review_items / get_review_item / get_review_history
       → append_review_event (only writable path)
```

| Operation | Event | Permission |
| --- | --- | --- |
| List / get / history | — | `legal_review.read` |
| Assign / reassign | `assigned` | `legal_review.assign` |
| Start review | `review_started` | `legal_review.decide` |
| Approve / reject / changes | decision events | `legal_review.decide` |
| Reopen | `reopened` | `decide` or `admin` |

Reuses `ALLOWED_TRANSITIONS`. Idempotent assign/start when state+assignee already match. Optimistic concurrency via `expected_row_version` → HTTP 409.

---

## 4. Authentication / authorization strategy

| Layer | Behavior |
| --- | --- |
| Service API key | Transport auth only (`AuthPrincipal`). **Never** a human reviewer. |
| Human auth mode | `disabled` (default) or `test_fixture` (non-production only) |
| Production | `LIFEPILOT_LEGAL_REVIEW_API_ENABLED` forced **off**; test_fixture forced **off** |
| Permissions | Server-side only; client cannot supply `actor_id` / `actor_kind` (schema `extra=forbid`) |
| Headers (test fixture) | `X-LifePilot-Reviewer-Id` + `X-LifePilot-Reviewer-Secret` |

**Integration dependency:** A production IdP / reviewer account system is still required before enabling decision routes outside tests. Until then, human decisions remain securely unavailable.

---

## 5. Reviewer decision validation

Approvals require:

- Non-empty rationale
- Confirmed evidence pin (`document_version_id` and/or fingerprint matching the item)
- Item not invalidated
- Pathway / requirement scope match when provided
- `acknowledge_effective_period` / `acknowledge_cohort_conditions` when the item carries those scopes
- Authenticated `human_reviewer` with `legal_review.decide`
- Server timestamp via `append_review_event`

No thresholds invented. No auto-approval of imported items.

---

## 6. API contracts and access control

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/v1/legal-review/items` | Filter + pagination |
| GET | `/v1/legal-review/items/{id}` | Item + history |
| GET | `/v1/legal-review/items/{id}/events` | Event history |
| POST | `/v1/legal-review/items/{id}/assign` | Assign |
| POST | `/v1/legal-review/items/{id}/start` | Start review |
| POST | `/v1/legal-review/items/{id}/decisions` | Approve / reject / changes |
| POST | `/v1/legal-review/items/{id}/reopen` | Reopen |

Default response when disabled: **503** `legal_review_api_disabled`.  
Human auth unavailable: **503** `human_auth_unavailable`.  
Missing/invalid reviewer credentials: **401**.  
Permission denied: **403**.  
Stale `row_version`: **409**.  
Forged body `actor_*`: **422**.

Existing `/v1/answer`, `/health`, `/ready` unchanged.

---

## 7. Legacy queue import results (synthetic / dry-run)

Importer: `import_high_risk_queue(..., dry_run=True)` by default.

Against production file `config/eval/high_risk_legal_review_queue.json` (dry-run on test DB):

- All current items **quarantined** (no `document_version_id` / `evidence_fingerprint`)
- File preserved read-only
- Zero review items created
- Legacy `verification_status` values (including `human_verified`) **never** become approvals

Synthetic enriched fixture tests:

| Outcome | Behavior |
| --- | --- |
| Import with explicit fingerprint | Creates **pending** `high_risk_queue_item` |
| Missing provenance | Quarantined |
| Repeat import | Duplicate |
| JSON `human_verified` | Recorded in event metadata only; workflow state remains `pending` |

**Do not run the importer against live `lifepilot`.**

---

## 8. Concurrency and audit guarantees

- All workflow mutations use `append_review_event` + `SELECT FOR UPDATE` + `row_version`
- Event UPDATE/DELETE triggers unchanged
- Assignment metadata embedded at INSERT time (no post-insert event UPDATE)
- Regression helper `assert_no_eventless_state_change` detects direct item writes
- Full DB-level ban on eventless item updates deferred to **10D.4**

---

## 9. Test results

| Suite | Result |
| --- | --- |
| `tests/test_legal_review_workflow.py` + governance | **25 passed** (focused run) |
| Full offline `tests/` | **778 passed, 5 skipped** |
| Live `lifepilot` | Unchanged at `20261113_0010` |

Covered: listing/filters, assign/start/decide/reopen, auth fail-closed, forged identity, service-key denial, pin mismatch, concurrency, non-promotion, JSON quarantine/import/duplicates, API 503/401/403/409/422, event integrity.

---

## 10. Known limitations → 10D.3–10D.5

| ID | Phase | Item |
| --- | --- | --- |
| D2-AUTH | — | Production human IdP still missing; routes stay disabled |
| D3-01 | 10D.3 | Automatic `source_version_changed` on new DocumentVersion |
| D3-02 | 10D.3 | Reassessment scheduling / maintenance alerts |
| D4-01 | 10D.4 | Gate LegalRule → `human_verified` on approved event + pin |
| D4-02 | 10D.4 | DB enforcement against eventless item updates |
| D4-03 | 10D.4 | API/agent refuse treating approved items as legal authority |
| D5-01 | 10D.5 | Integrated governance acceptance |

---

## 11. Stop confirmation

Phase **10D.2 only** is complete. No 10D.3+ automation, no LegalRule promotion, no live migration, no commercial auth/tenancy, no fabricated real legal approvals.
