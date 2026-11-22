# Phase 10C.3 — Qualifier, Exception and Conditional Claim Verification

**Status:** Implementation complete for review  
**Date:** 2026-11-22  
**Live DB:** Unchanged (`alembic_version=20261113_0010`; counts 14/80/601/678/9883)  
**Extra default LLM calls:** None

---

## 1. Issue register (pre-implementation)

| ID | Defect | Severity | Disposition |
| --- | --- | --- | --- |
| Q-01 | Lexical overlap ≠ entailment; inverted conditions can pass | Critical | Mitigated via polarity/bound/geo/temporal conflicts |
| Q-02 | Polar antonym coverage too narrow (3 pairs) | Critical | Expanded axes in `qualifiers.py` |
| Q-03 | Negation not claim↔evidence polarity; stripped “if” conditions | Critical | Fixed: conditional exemption / exception detectors |
| Q-04 | Evidence conditionals need not appear in claim | Critical | Fixed with supporting-span scoped mandatory checks |
| Q-05 | Truncation gate exclusivity-only | High | Extended to non-exclusive eligibility/requirement claims |
| Q-06 | Cross-passage combination unchecked | High | Multi-passage cohort/pathway/section gates |
| Q-07 | Prose can merge/drop non-temporal qualifiers | High | Material qualifier preservation + high-risk rebuild |
| Q-08 | Unsupported universals incompletely guarded | High | `unsupported_universal_claim` |
| Q-09 | No at-least / at-most bound semantics | High | Bound constraints via numeric parser + local cues |
| Q-10 | Corroboration can reinforce weak lexical support | Medium | Deferred (still requires overlap+numeric; not expanded) |
| LP-10C2-009 | Full qualifier & entailment verification | — | Closed by this phase |

---

## 2. Modified and added files

### Added
- `backend/lifepilot/agent/qualifiers.py` — structured qualifier recognition + entailment
- `tests/test_qualifier_conditional_verification.py` — 24 focused tests
- `docs/phase_10c3_qualifier_verification_report.md` — this report

### Modified
- `backend/lifepilot/agent/verification.py` — wire qualifier assessment; drop unsafe insufficiencies; shared cohort/conditional patterns
- `backend/lifepilot/agent/grounding.py` — defense-in-depth qualifier drops; prose qualifier preservation; high-risk rebuild
- `docs/README.md` — index entry for this report

---

## 3. Confirmed defects and fixes

1. **Stripped conditional exemptions** — “do not need sponsorship if …” no longer supports “do not need sponsorship.”
2. **Exception omission** — “required unless …” no longer supports bare “required.”
3. **Polarity inversions** — eligible/ineligible, required/exempt, must/must not → `CONTRADICTED`.
4. **Bound inversions** — at least vs at most for the same numeric token → `CONTRADICTED`.
5. **Unsupported universals** — “all applicants / without exception” against conditional evidence → dropped.
6. **Cross-passage cohort merge** — distinct cohort passages cannot jointly support an unscoped claim.
7. **Truncation** — truncated excerpts mark eligibility/requirement claims uncertain beyond exclusivity-only.
8. **Prose leakage** — suppressed unconditional claims do not survive final answer reconstruction.
9. **False-positive guard** — general threshold claims (e.g. X01 £41,700) are not forced to inherit unrelated transitional clauses that govern a different figure in the same passage (supporting-span scoping).

---

## 4. Qualifier recognition design

`QualifierProfile` captures bounded cues:

- Cohorts (transitional, age bands, new entrant, PhD, London, healthcare, …)
- Conditionals (`only if`, `unless`, `except`, `subject to`, `provided that`, …)
- Polarities (eligibility, permission, requirement, must/must not)
- Bound directions (at least / at most) paired with `extract_numeric_quantities`
- Temporal before/after, geographic inside/outside
- Age / education / occupation / sponsorship / salary cues
- Universal markers; unrecognized opaque condition hints (explicit, not silent drop)

Unknown/opaque conditions surface as `unrecognized_evidence_conditions_*` rather than being discarded.

---

## 5. Contradiction and entailment safeguards

Decision policy in `assess_claim_qualifier_entailment`:

| Decision | Examples |
| --- | --- |
| `contradicted` | polarity / bound / temporal / geographic inversions |
| `insufficient` | stripped conditions, unsupported universals, cross-passage merges, omitted mandatory conditions on supporting spans |
| `uncertain` | truncation may hide qualifiers; opaque further-conditions language on strong claims |
| `pass` | no deterministic conflict |

Reason codes remain snake_case and machine-readable. This is **not** unrestricted NLI; complex cases abstain or qualify.

---

## 6. Multi-passage verification behavior

- Distinct non-empty cohorts across citations + unscoped claim → `cross_passage_distinct_cohorts`
- Distinct hosts/versions combining conditionals into an unconditional claim → blocked
- Unrelated heading paths without adjacent char spans → blocked when combining conditionals
- Each passage independently supporting the claim remains allowed when no distinct-cohort conflict exists
- Character proximity alone is never treated as proof of logical applicability

---

## 7. Final prose safety behavior

`compose_grounded_answer_text`:

- Preserves material conditional/bound/negation cues from kept claims
- Forces rebuild from claims when high-risk multi-claim answers or conditional high-risk assertions are present
- Filters sentences that invent high-risk conclusions from weak per-claim overlap
- Rejected/suppressed claims do not re-enter prose; numeric scrubbing from prior phases remains intact

---

## 8. Focused test results

`tests/test_qualifier_conditional_verification.py`: **24 passed**

Coverage includes eligible/ineligible, required/exempt, must/must not, at least/at most, before/after, inside/outside, sponsorship exemptions, age/occupation/transitional, missing conditions, valid paraphrases, universals, negation scope, multi-passage, truncation, cross-pathway, currency/unit, prose leakage, grounding drop, zero LLM calls, and false-positive guards.

---

## 9. Full offline regression results

- Related pre-full gate: **115 passed, 1 skipped**
- Full offline suite: **720 passed, 5 skipped** (prior baseline 696 passed, 5 skipped; +24 new Phase 10C.3 tests)

Live database verification: **unchanged** (`20261113_0010`; 14/80/601/678/9883).

---

## 10. Known limitations and recommendations for Phase 10C.4

1. **Not general legal entailment** — regex/profile checks miss subtle paraphrases and nested multi-clause logic.
2. **Corroboration path (Q-10)** — still lexical+numeric; could require qualifier pass on corroborators.
3. **API provenance** — claim-level structured qualifier/provenance payloads belong in Phase 10C.4.
4. **Reviewed LegalRule cohort packaging** — continue ensuring cohort keys always appear in claim text for human-verified rules (already partially gated).
5. **Table-row condition binding** — richer row/header binding would further reduce false combines.
6. **Eval expansion** — add held-out qualifier omission/inversion cases without retuning on X16–X20.

---

## Acceptance checklist

- [x] Qualifier omission / inversion bugs fixed (with supporting-span false-positive guard)
- [x] Supported conditions remain attached to retained claims
- [x] Unsupported universal/eligibility conclusions suppressed or qualified
- [x] Cross-passage unsupported combinations blocked
- [x] Truncated evidence cannot silently justify comprehensive claims
- [x] Numeric / temporal / pathway / authority safeguards intact (related suites green)
- [x] Valid conditional paraphrases and X01/X16 transitional claims not unnecessarily suppressed
- [x] No live DB changes
- [x] No extra default LLM calls
