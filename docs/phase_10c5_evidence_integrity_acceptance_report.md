# Phase 10C.5 — Integrated Evidence Integrity Acceptance Report

**Date:** 2026-11-22  
**Acceptance decision:** **PASS WITH LIMITATIONS**  
**Live DB:** Unchanged (`alembic_version=20261113_0010`; `document_chunks=9883`)  
**Extra default LLM calls:** None  
**Engineering vs legal:** This report accepts *engineering evidence-integrity*. It does **not** certify independently verified legal accuracy of immigration requirements.

---

## 1. Executive summary

Phase 10C.1–10C.4 safeguards were exercised through the real agent path (orchestrator → generation → verification → prose → `ClaimEvidenceRef` → `/v1/answer` serialization) using synthetic fixtures and mocked LLM/connectors.

One confirmed Phase 10C defect was found and fixed during acceptance:

- **Uncertain / truncated / unclear-status corroboration could still raise high-risk confidence** (follow-on to Q-10). Affirmative independent corroboration now requires qualifier `PASS`, non-truncated evidence, and a clear enacted-like `legal_status`.

Full offline suite: **753 passed, 5 skipped** (prior baseline 738; +15 acceptance tests).

**Phase 10C may close for engineering purposes** under the limitations in §7 and blockers in §13. Phase 10D/10E remain separate.

---

## 2. Integrated architecture review

```
API /v1/answer
  → AgentRequest (auto external mode; verification required)
  → temporal context (as_of_date / resolved_reference_date)
  → EvidenceOrchestrator (internal RAG + optional external)
  → EvidenceItem normalization (authority, truncation, temporal_provenance)
  → generate_grounded_answer (single structured LLM call when evidence exists)
  → validate_and_ground_answer (overlap, numeric, pathway, qualifier)
  → verify_material_claims
       numeric → temporal → pathway → qualifier → corroboration → prose rebuild
  → ClaimVerificationOutcome.evidence_refs (ClaimEvidenceRef)
  → AgentResponse
  → AnswerResponse (verification + claim_verification; evidence gated)
```

### Integration issue register (pre-fix)

| ID | Finding | Severity | Result |
| --- | --- | --- | --- |
| I-01 | `search_distinct_corroboration` treated qualifier `UNCERTAIN` as eligible for positive corroboration | Critical | **Fixed** |
| I-02 | Truncated corroborator passages could still raise confidence | High | **Fixed** |
| I-03 | `legal_status=unknown` could still grant independent corroboration | High | **Fixed** |
| I-04 | Claim dates without corroborator period could still match on amount alone | Medium | **Fixed** (`missing_effective_period`) |
| I-05 | HTTP `evidence=null` in compact mode — essential status must not hide | Medium | **Verified OK** via `claim_verification` |
| I-06 | Live DB still at `0010` while repo has `0011` temporal migration | Ops dependency | **Documented** — not a 10C code blocker for non-LegalRule paths |
| I-07 | Unrestricted NL entailment still out of scope | Known limitation | Carried to 10D/10E |

Metadata continuity checks: authority category, truncation flags, temporal/cohort fields, and document/version/chunk IDs survive verification outcomes and detailed API projection. Compact mode strips excerpts only.

---

## 3. Test dataset and methodology

| Aspect | Approach |
| --- | --- |
| Isolation | Synthetic `EvidenceItem`s via orchestrator override; no writes to live `lifepilot` |
| LLM | Counting fake structured provider (exactly one call when evidence present; zero when empty) |
| HTTP | FastAPI `TestClient` on `/v1/answer` with auth key |
| Assertions | Structured statuses, reason codes, retained flags, API field presence, excerpt authenticity |
| Legal accuracy | **Not evaluated** — synthetic wording only |

Primary suite: `tests/test_phase_10c5_evidence_integrity_acceptance.py`  
Plus full Phase 10B / 10C.1–10C.4 / API / verification reliability regressions.

---

## 4. Scenario-by-scenario results

| # | Scenario | Result |
| --- | --- | --- |
| 1 | Currency / number / unit handling | Pass |
| 2 | EU decimal-comma (`€ 423,00` → €423.00) | Pass |
| 3 | Wrong-currency rejection | Pass |
| 4 | Monthly vs annual mismatch | Pass |
| 5–6 | Future / expired framing | Pass (uncertainty or reject) |
| 7 | Transitional vs general coexistence (X01/X16 class) | Pass |
| 8 | Conditional eligibility preservation | Pass |
| 9 | Negation / bound inversion | Pass |
| 10 | Unsupported universals | Pass |
| 11 | Cross-pathway contamination | Pass |
| 12 | Truncated supporting passages | Pass |
| 13 | Table-header interpretation | Pass (covered in 10C.2 suite; not re-broken) |
| 14 | Statistical dataset normative claims | Pass |
| 15 | Proposed / withdrawn sources | Pass |
| 16 | Same-document pseudo-corroboration | Pass |
| 17 | Independent compatible corroboration | Pass (requires `in_force` + qualifier PASS) |
| 18 | Missing mandatory qualifiers in corroboration | Pass |
| 19 | Uncertain corroboration must not raise confidence | **Pass after fix** |
| 20 | Claim-level source/version traceability | Pass |
| 21 | Authentic supporting excerpts | Pass |
| 22 | Missing external identifiers stay null | Pass |
| 23–24 | Compact vs detailed API modes | Pass (decision parity) |
| 25–26 | Rejected-claim leakage (prose + API) | Pass |
| 27–28 | Missing evidence / abstention | Pass (0 LLM calls) |
| 29 | Legacy client fields | Pass |
| 30 | No additional default LLM calls | Pass |

---

## 5. Confirmed defects

1. **I-01/I-02/I-03/I-04** — Affirmative corroboration could be granted under uncertain qualifier assessment, truncated passages, unclear legal status, or missing period for dated claims.

---

## 6. Defects fixed

**File:** `backend/lifepilot/agent/verification.py` (`search_distinct_corroboration`)

- Require qualifier decision **`PASS` only** (`CONTRADICTED` / `INSUFFICIENT` / `UNCERTAIN` rejected).
- Reject truncated corroborators.
- Reject `legal_status` in `{unknown, ""}` for affirmative confirmation.
- Reject missing effective-period cues when the claim itself is date-scoped.
- Record machine-readable `corroboration_rejected:*` reasons.

**Regression:** `test_uncertain_truncated_and_unclear_status_cannot_raise_corroboration` in the 10C.5 suite.

---

## 7. Remaining limitations

1. Deterministic checks are **not** general legal entailment.  
2. Migration **`20261118_0011` is not applied** on live DB — curated LegalRule temporal features depend on future ops deploy.  
3. No live connector / real-model evaluation in this phase.  
4. No independent human legal accuracy review (Phase 10E / 10D).  
5. Canonical-URL independence is path/hash/document based; deeper mirror graphs remain 10D-scale.  
6. `pre_verification_claims` remain internal (intentional anti-leakage).

---

## 8. Performance comparison

Measured in `test_performance_verification_provenance_and_corroboration_overhead` (soft CI bounds):

| Operation | Soft bound | Notes |
| --- | --- | --- |
| 50× `verify_material_claims` (2 evidence items) | &lt; 15s | Includes corroboration search |
| 100× `search_distinct_corroboration` | &lt; 5s | Qualifier PASS gate added |
| Provenance JSON projection | &lt; 0.5s; &lt; 50KB | Bounded excerpts |
| Detailed `/v1/answer` payload | &lt; 250KB | Long-passage case |

No caching of verification freshness was introduced. Qualifier-gated corroboration adds CPU proportional to candidate count; no extra LLM/network by default.

---

## 9. API compatibility results

| Field | Compact (`INCLUDE_EVIDENCE=0`) | Detailed (`=1`) |
| --- | --- | --- |
| `verification` | Full report | Same decisions |
| `claim_verification` | Status/reasons/refs; **no excerpts** | + authenticated excerpts |
| `evidence` | `null` (legacy) | Detailed projection |
| `claims` | Retained only | Identical to compact |
| Decision parity | Statuses match across modes | Verified |

Human-reviewed source refs still set `mechanical_match_only=true` and an explicit non-promotion limitation.

---

## 10. Full regression test results

| Suite | Result |
| --- | --- |
| Phase 10C.5 acceptance | **15 passed** |
| Related 10B–10C.4 + API + reliability | **137 passed** (combined gate) |
| **Full offline repository** | **753 passed, 5 skipped** |
| Live DB mutations | **None** |
| Real model/connector eval | Not run |
| Human-verified legal accuracy | Not run |

Unit vs integration: acceptance mixes direct `verify_material_claims` unit/integration checks with mocked-HTTP `/v1/answer` E2E. Both are synthetic.

---

## 11. Carry-forward issue register

| ID | Item | Owner milestone |
| --- | --- | --- |
| CF-01 | Deploy/verify Alembic `20261118_0011` on staging then live | Ops / pre-10D |
| CF-02 | Human legal review governance for high-risk rules | **10D** |
| CF-03 | Live legal accuracy benchmarks on held-out cases | **10E** |
| CF-04 | Richer mirror/CDN source-identity registry | 10D |
| CF-05 | Optional compact qualifier-profile dump for eval tooling | 10C.5+/10D |
| CF-06 | Broader NL entailment remains abstain/uncertain | 10E research |

---

## 12. Acceptance decision

### **PASS WITH LIMITATIONS**

**Rationale**
- No known *critical* evidence-integrity defect remains in the tested engineering scope after I-01–I-04 remediation.
- Numeric, currency, pathway, temporal, qualifier, provenance, API leakage, and corroboration uncertainty gates pass the defined suites.
- Compact/detailed verification decisions are equivalent.
- Source authority ≠ claim-level legal approval is preserved.
- Limitations are operational (migration deploy) and methodological (no human legal certification), not unfixed critical code defects in 10C scope.

---

## 13. Explicit blockers before Phase 10D

1. **Do not treat Phase 10C close as legal clearance** of immigration guidance.  
2. Plan **safe application of migration `0011`** (cloned/staging first; never ad-hoc on live without backup).  
3. Phase **10D** must define human review workflows before promoting high-risk claims beyond mechanical verification.  
4. Phase **10E** must supply independent legal accuracy evaluation — out of scope here.  
5. No historical corpus backfill or live data mutation should begin solely because 10C.5 passed.

---

## Modified / added files (10C.5)

| Path | Change |
| --- | --- |
| `backend/lifepilot/agent/verification.py` | Corroboration uncertainty / truncation / legal-status / period gates |
| `tests/test_phase_10c5_evidence_integrity_acceptance.py` | Integrated acceptance suite |
| `docs/phase_10c5_evidence_integrity_acceptance_report.md` | This report |
| `docs/README.md` | Index entry |

---

## Stop

Phase 10C.5 complete. Not started: Phase 10D, Phase 10E, live migrations, historical backfills, commercial tenancy/billing.
