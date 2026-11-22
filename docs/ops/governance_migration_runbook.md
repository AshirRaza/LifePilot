# Operator runbook — Legal review governance migrations (0011→0014)

**Audience:** staging / private ops  
**Live `lifepilot`:** Do **not** apply until an explicit maintenance window. Current live stamp remains `20261113_0010` until approved.

## Chain

```text
20261113_0010  (live baseline)
  → 20261118_0011  temporal legal_rules / document version legal fields
  → 20261122_0012  legal_review_items / legal_review_events (+ append-only UPDATE/DELETE triggers)
  → 20261122_0013  evented item updates + human_verified linkage triggers
  → 20261122_0014  review-event INSERT gate + lp_insert_legal_review_event + approved-event hardening
```

## Pre-flight (staging-first)

1. Take a `pg_dump` of the target database; verify restore to an isolated `*_test` / staging clone.
2. Confirm Alembic `down_revision` chain is linear (`alembic history`).
3. Confirm app build includes `lp_insert_legal_review_event` Python caller (`lifepilot.db.legal_review`).
4. Confirm `LIFEPILOT_LEGAL_REVIEW_API_ENABLED=0` and auth mode `disabled` (or non-prod `test_fixture` only).
5. Confirm `LIFEPILOT_LEGAL_VERIFICATION_POLICY` is intentional (`advisory` or `strict_verified_only`).

## Upgrade (staging)

```bash
# From repo root, with DATABASE_URL pointed at the STAGING database only
alembic upgrade 20261122_0014
```

Verify:

```sql
SELECT version_num FROM alembic_version;
-- expect 20261122_0014

SELECT tgname FROM pg_trigger
 WHERE tgrelid = 'legal_review_events'::regclass;

SELECT rolname FROM pg_roles WHERE rolname = 'lifepilot_gov_writer';

SELECT proname FROM pg_proc WHERE proname = 'lp_insert_legal_review_event';
```

Smoke:

- API `/health` and `/ready`
- One RAG retrieval against an existing jurisdiction
- Create pending review item via test-fixture auth **only if** staging auth mode allows it
- Confirm direct `INSERT INTO legal_review_events` fails; `SELECT lp_insert_legal_review_event(...)` works

## Rollback / recovery

Prefer restore-from-dump for production incidents.

Alembic downgrade (staging):

```bash
alembic downgrade 20261122_0013   # remove insert gate
# or further:
alembic downgrade 20261118_0011
```

**DBA break-glass (events table):**

```sql
BEGIN;
ALTER TABLE legal_review_events DISABLE TRIGGER USER;
-- corrective DML / cleanup
ALTER TABLE legal_review_events ENABLE TRIGGER USER;
COMMIT;
```

Record an `administrative_correction` rationale in ops notes. Trigger disable is **not** human identity proof.

## Privilege model (0014)

| Principal | Intent |
| --- | --- |
| Ordinary app login | SELECT/UPDATE corpus tables; **no** direct INSERT into `legal_review_events` (gate trigger) |
| `lifepilot_gov_writer` | Owns `lp_insert_legal_review_event` (SECURITY DEFINER); granted INSERT on events |
| Superuser / table owner | Can `DISABLE TRIGGER USER` — operational recovery only |

Optional later hardening: create a non-superuser app login and revoke table ownership privileges (Phase 14 / ops).

## What this runbook does **not** do

- Enable production reviewer API / IdP
- Backfill historical LegalRule approvals
- Schedule reassessment/maintenance cron
- Migrate live `lifepilot` without an approved window
