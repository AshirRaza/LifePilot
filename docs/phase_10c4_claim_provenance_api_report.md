# Phase 10C.4 — Claim-Level Provenance, API Transparency & Corroboration Integrity

**Status:** Implementation complete for review  
**Date:** 2026-11-22  
**Live DB:** Unchanged (`alembic_version=20261113_0010`)  
**Extra default LLM calls:** None

---

## 1. Issue register

| ID | Gap | Disposition |
| --- | --- | --- |
| G1 | HTTP evidence default-off strips almost all provenance when on | Fixed: detailed projection when `LIFEPILOT_API_INCLUDE_EVIDENCE`; essential status always via `claim_verification` |
| G2 | No typed claim→evidence ref | Fixed: `ClaimEvidenceRef` |
| G3 | No structured claim verification view for HTTP | Fixed: additive `AnswerResponse.claim_verification` |
| G4 | Qualifier assessment only as reason strings | Retained as reason codes on outcomes (profiles not dumped — size/privacy) |
| G5 | Corroborator evidence id only in reason string | Fixed: `corroborating_evidence_id` + corroborating `evidence_refs` |
| G6 | **Q-10** corroboration skips qualifier entailment | Fixed: `search_distinct_corroboration` |
| G7 | Rejected claims must not leak via API metadata | Fixed: retained-only projection + empty refs on suppressed outcomes |
| G8 | No INCLUDE_EVIDENCE / claim provenance tests | Fixed: `tests/test_claim_provenance_api.py` |
| LP-10C3-Q10 | Corroboration lexical+numeric only | Closed |

---

## 2. Modified and added files

### Added
- `backend/lifepilot/agent/claim_provenance.py`
- `tests/test_claim_provenance_api.py`
- `docs/phase_10c4_claim_provenance_api_report.md`

### Modified
- `backend/lifepilot/agent/verification.py` — outcome provenance fields; Q-10 corroboration search
- `backend/lifepilot/api/schemas.py` — additive `claim_verification`, `evidence_detail`
- `backend/lifepilot/api/app.py` — projection in `_to_answer_response`
- `docs/README.md` — index entry

---

## 3. Provenance contract (`ClaimEvidenceRef`)

Additive structured reference per supporting evidence item:

| Field group | Examples |
| --- | --- |
| Identity | `evidence_id`, `source_type`, `source_url`, `canonical_url` |
| Authority | `source_name`, `issuing_authority`, `source_authority_category`, `jurisdiction`, `pathway_ids` |
| Document lineage | `document_id`, `document_version_id`/`number`, `chunk_id`/`index`, `heading_path`, `char_start`/`end`, `content_sha256` |
| Excerpt | `excerpt`, `excerpt_authenticated`, `excerpt_truncated` |
| Temporal / cohort | `effective_from`/`to`, `cohort_key`, `is_transitional`, `legal_status`, timestamps |
| Review | `verification_state`, `human_review_state` |
| Limitations | `is_truncated`, `truncation_reason`, `provenance_limitations`, `mechanical_match_only` |

**Rules**
- Excerpts are real substrings of retrieved `passage_text` (bounded ≤480 chars). Never LLM-invented.
- Missing external document/chunk IDs stay `null` with explicit limitation codes — never fabricated.
- `human_verified_source` does **not** imply claim-level legal approval (`mechanical_match_only=true` + limitation note).
- Existing `AnswerClaim.evidence_ids` unchanged for LLM/prompt compatibility.

`ClaimVerificationOutcome` additive fields: `evidence_refs`, `corroborating_evidence_id`, `provenance_incomplete`, `corroboration_rejection_reasons`.

---

## 4. API request/response compatibility

| Field | Before | After |
| --- | --- | --- |
| `verification` | Full report dump | Unchanged |
| `claims` | Retained claims only | Unchanged |
| `evidence` | `null` unless INCLUDE_EVIDENCE (5 fields) | `null` unless INCLUDE_EVIDENCE (**detailed** projection) |
| `claim_verification` | — | **New**, always when verification ran; retained claims only; excerpts only if INCLUDE_EVIDENCE |
| `evidence_detail` | — | `none` \| `detailed` |

**Compact mode** (`LIFEPILOT_API_INCLUDE_EVIDENCE=false`):
- `evidence=null` (backward compatible)
- `claim_verification` still exposes status, reasons, uncertainty, corroboration, authority/lineage refs **without** passage excerpts

**Detailed mode** (`true`):
- Full evidence list with passage (bounded), lineage, temporal/cohort
- Claim refs include authenticated excerpts

No breaking removals; new fields are optional/additive (`extra=forbid` with defaults).

---

## 5. Q-10 remediation report

`search_distinct_corroboration()` (wraps legacy `find_distinct_corroboration`) now requires:

1. Distinct URL key  
2. **Not** same underlying document (`document_id` / `document_version_id` / `content_sha256` / canonical URL)  
3. Adequate authority + enacted legal status  
4. Matching jurisdiction  
5. Numeric + currency + unit support  
6. Pathway signal compatibility  
7. Lexical overlap  
8. **`assess_claim_qualifier_entailment` must not contradict or be insufficient**  
9. Shared cohort when the claim is cohort-scoped  
10. Compatible temporal period when both sides expose dates  

Failures record `corroboration_rejected:{id}:{reason}` and do not grant `reused_retrieval_distinct_source`.

---

## 6. Verification diagnostic schema (stable reason codes)

Existing snake_case codes remain; newly emphasized for API consumers:

| Area | Example codes |
| --- | --- |
| Numeric / currency / unit | `currency_mismatch:*`, `unit_period_mismatch:*`, `numeric_literal_not_in_passage` |
| Qualifier | `polarity_conflict:*`, `bound_direction_conflict:*`, `mandatory_condition_omitted_from_claim:*`, `unsupported_universal_claim` |
| Temporal | `future_effective_*`, `expired_or_sunset_*`, `cohort_restriction_omitted_from_claim` |
| Pathway | `pathway:*`, `related_context_numeric_not_explicit_comparison` |
| Authority | `statistical_dataset_cannot_verify_normative_requirement`, `portal_page_*`, `eu_level_*` |
| Truncation | `cited_excerpt_truncated_qualifiers_may_be_omitted`, `truncated_evidence_may_omit_qualifiers` |
| Corroboration | `corroboration_rejected:*:same_underlying_document`, `:cohort_mismatch`, `:qualifier_*`, `:numeric_incompatible`, `:pathway_incompatible` |
| Insufficient | `insufficient_passage_overlap`, `citation_unresolved_or_cross_jurisdiction` |

User-facing uncertainty strings stay separate from machine reason codes.

---

## 7. Focused test results

`tests/test_claim_provenance_api.py`: **18 passed**

Covers ID resolution, internal lineage, external missing IDs, excerpt authenticity, truncation, temporal/cohort, authority/human-review non-promotion, qualifier/pathway diagnostics, independent vs same-document corroboration, cohort/period/condition mismatches, API leakage guards, compact/detailed modes, size bounds, zero LLM calls, numeric diagnostics.

---

## 8. Full offline regression results

- Related gate: **122 passed**
- Full offline suite: **738 passed, 5 skipped** (prior baseline 720 passed, 5 skipped; +18 Phase 10C.4 tests)

Live DB: **unchanged** (`20261113_0010`).

---

## 9. Performance observations

- No extra LLM calls; provenance is pure Python over already-loaded `EvidenceItem`s.
- Excerpt/passage bounding (480 / 1200 chars) keeps serialization modest; focused size test asserts payload &lt; 200KB for a long passage case.
- Corroboration now runs qualifier assessment per candidate — same cost class as main verification path; bounded by evidence list size (no network unless optional corroborator enabled).
- Prompt packing unchanged: still uses existing evidence IDs; internal DB IDs are not newly injected into the LLM prompt.

---

## 10. Known limitations → Phase 10C.5

1. Qualifier **profiles** are not serialized (only reason codes) — consider a compact optional dump for eval tooling.  
2. `pre_verification_claims` remain internal (not on HTTP) by design to avoid rejected-claim leakage.  
3. Canonical URL independence is path-based; deeper mirror/CDN equivalence may need a source registry in 10D.  
4. Integrated end-to-end acceptance of provenance UX belongs in **10C.5**.  
5. Human review workflows / claim-level legal certification belong in **10D**, not mechanical provenance.

---

## Acceptance checklist

- [x] Retained claims have resolvable evidence refs (or `provenance_incomplete`)
- [x] Excerpts authentic and bounded
- [x] Internal document/version/chunk survives detailed API serialization
- [x] External evidence keeps null IDs without fabrication
- [x] Verification + uncertainty exposed via stable structured fields
- [x] Rejected claims do not leak through API claims/claim_verification/answer
- [x] Corroboration requires independence + compatible legal scope / qualifiers
- [x] Backward compatible (`verification`, `evidence=null` default)
- [x] No live DB changes; no extra default LLM calls
