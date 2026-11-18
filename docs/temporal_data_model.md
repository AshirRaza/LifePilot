# LifePilot — Temporal Legal Data Model (Phase 10B.1)

This document defines the schema semantics and invariants introduced in Phase 10B.1 (`alembic/versions/20261118_0011_temporal_legal_data_model.py` and `backend/lifepilot/db/models.py`).

## 1. Core Distinctions: Crawl Lifecycle vs. Legal Validity

LifePilot separates **source retrieval lifecycle** (`document_versions`) from **rule-level legal applicability** (`legal_rules`):

| Concept | Column / Table | Meaning | Never Conflate With |
| --- | --- | --- | --- |
| Retrieval timestamp | `document_versions.retrieved_at` | When LifePilot fetched those raw bytes. | Legal effective date or source publication date. |
| Unchanged recheck | `document_versions.last_checked_at` | Last unchanged verification fetch (migration `0010`). | `retrieved_at` or proof that legal rules are static. |
| Crawl currency | `document_versions.is_current` | Latest promoted snapshot for that URL (`uq_document_versions_one_current`). | Legally in force (`archived` URLs can have `is_current=true`). |
| Observed HTTP/source dates | `http_last_modified`, `http_etag`, `source_published_at`, `source_updated_at` | Observations from headers, HTML metadata, or PDF properties. | Authoritative legal effective dates (`effective_from`/`effective_to`). |
| Source legal status | `document_versions.legal_status` | Controlled classification (`unknown`, `in_force`, `historical`, `superseded`, `future_pending`, `proposed`, `withdrawn`, `archived`, `transitional`). | Defaults to `'unknown'` for existing and unassessed versions. |
| Archival / supersession | `archived_at`, `superseded_at`, `superseded_by_version_id` | Explicit archival or version-to-version supersession tracking. | Automatic overwriting of historical `DocumentVersion` rows. |
| Legal rule validity | `legal_rules.{effective_from, effective_to, effective_from_inclusive, effective_to_inclusive, effective_date_status}` | When a specific requirement or threshold legally applies. | Crawl timestamps or publication timestamps. |

## 2. Temporal Invariants (Enforced in PostgreSQL & Schema Design)

1. **Unknown effective dates are never treated as universally applicable.**
   - `legal_rules.effective_date_status` defaults to `'unknown'` (requiring `effective_from IS NULL AND effective_to IS NULL`).
   - Query/application layers must not assume `NULL` bounds mean "valid for all dates" unless `effective_date_status` explicitly indicates `'known_open_ended'` (`effective_from IS NOT NULL`).
   - `'partially_known'` marks rules where one bound is confirmed and the other remains unconfirmed.
2. **A current document can contain historical or future rules.**
   - One official GOV.UK or agency page (`DocumentVersion.is_current = true`) frequently states current thresholds, pre-cutoff transitional thresholds, and upcoming changes together. Modeling `legal_rules` at rule granularity (linked via `document_version_id`) avoids conflating page recency with rule applicability.
3. **A historical document can still support a historical query.**
   - Prior `DocumentVersion` rows (`is_current = false`, `legal_status IN ('historical', 'superseded')`) remain immutable in `document_versions` and can be linked directly by `legal_rules.document_version_id`.
4. **A newer crawl does not automatically supersede an earlier legal requirement.**
   - Fetching a new `DocumentVersion` does not mutate `effective_to` or `verification_status` on existing `legal_rules` without explicit review/governance handling.
5. **Every legal rule retains official-source provenance.**
   - `ck_legal_rules_requires_source_provenance` enforces that at least one of `document_version_id`, `document_id`, or `source_id` is non-null.
   - Trigger `trg_legal_rules_provenance_consistency` guarantees `legal_rules.document_id` cannot disagree with `document_versions.document_id`.
6. **Referencing an official document never implies human verification.**
   - `legal_rules.review_status` defaults to `'unreviewed'` and `verification_status` defaults to `'unverified'`.
   - `ck_legal_rules_human_verified_requires_audit` rejects `verification_status = 'human_verified'` unless `review_status = 'approved'`, `reviewed_by` is non-empty, `reviewed_at` is set, and `document_version_id` is pinned.
7. **Multiple cohort-specific rules may coexist without overwriting or contradicting each other.**
   - Uniqueness is restricted to `(rule_key, rule_revision)` so a rule lineage can evolve across revisions (`supersedes_rule_id`) while multiple cohorts (e.g., `cohort_key = 'general'` and `cohort_key = 'transitional_pre_2024_cos'`) coexist for the same `(jurisdiction_code, pathway_id, requirement_category, effective_from)`.
8. **Exact decimal precision & boundary semantics.**
   - Monetary amounts and numeric thresholds use `NUMERIC(18, 4)` (`Decimal`, never binary float), paired with ISO-4217 uppercase 3-letter `currency_code`, controlled `amount_unit`, `numeric_comparator`, and optional `numeric_max_value`.
   - Date intervals support inclusive (`[from, to]`) and exclusive (`[from, to)`, e.g., *"applications made before 4 April 2030"*) bounds via `effective_from_inclusive` / `effective_to_inclusive`, with `ck_legal_rules_effective_bounds_order` forbidding empty or inverted intervals.

## 3. Why `document_chunks` Was Not Modified in Phase 10B.1

Adding `effective_from` / `effective_to` directly onto `document_chunks` in Phase 10B.1 would duplicate rule-level legal validity (a single chunk can describe multiple rules with different dates) without an active retrieval query consumer yet. `document_chunks.document_version_id` already carries an indexed foreign key to `document_versions` (`ix_document_chunks_document_version_id`), making source lifecycle metadata joinable when Phase 10B.2/10B.3 introduces temporal retrieval.

## 4. Migration Safety & Compatibility

- Migration `20261118_0011_temporal_legal_data_model.py` (`down_revision = "20261113_0010"`) is strictly **additive and reversible**.
- Existing `document_versions` rows receive `legal_status = 'unknown'` and `NULL` for all observed/archival timestamps; `retrieved_at`, `last_checked_at`, and `is_current` remain untouched.
- Existing `documents`, `document_chunks`, and `document_chunk_embeddings` rows are preserved untouched.
- `downgrade()` removes `legal_rules` (with its triggers and indexes) and drops the new `document_versions` columns/constraints/indexes without dropping any pre-existing data.
