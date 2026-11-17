# LifePilot scripts

Operational and evaluation CLIs. Prefer `PYTHONPATH=backend` (or activate `.venv`
from the repo root). Evaluation adapters that force `external_only` are diagnostic-only
and are not production modes.

Stable entry points referenced by docs, Compose, or cron keep their current paths.

## 1. Core operational commands

| Script | Purpose |
| --- | --- |
| `ask_lifepilot.py` | Full agent ask (hybrid retrieval + grounded answer + verification) |
| `run_api.py` | Private FastAPI server (`/v1/answer`, health/ready, ops status) |
| `run_agent_evidence.py` | Evidence orchestration only (no LLM required) |
| `query_internal_knowledge.py` | Internal hybrid RAG |
| `query_external_knowledge.py` | Official external connectors (`unverified_external`) |

## 2. Knowledge-base maintenance

| Script | Purpose |
| --- | --- |
| `run_knowledge_maintenance.py` | Bounded KB recheck / classify / queue material changes |
| `run_source_freshness_check.py` | Freshness ages from `DocumentVersion` |
| `run_ingestion.py` / `run_chunk_documents.py` / `run_embed_chunks.py` | Corpus build pipeline |
| `run_national_official_smoke.py` | NL/IE/FR/SG connector smoke |

Schedule maintenance via Task Scheduler/cron/Compose `--profile maintenance` (not auto-installed).

## 3. Evaluation and benchmarking

| Script | Purpose |
| --- | --- |
| `run_grounded_qa_eval.py` | Original 50-question grounded QA eval |
| `run_comparative_answer_eval.py` | Phase 9D.1 three-mode comparison |
| `run_unified_verification_eval.py` | Phase 9D.2 targeted verification eval |
| `run_verification_reliability_eval.py` | Phase 9D.3 offline claim-level verifier suite |
| `run_verification_reliability_e2e.py` | Phase 9D.3 bounded live agent E2E |
| `build_verification_reliability_fixtures_9d3.py` | Rebuild 9D.3 synthetic/real fixtures |
| `run_performance_optimization_9d4.py` | Phase 9D.4 paired latency/cost benchmark |
| `run_performance_microbench_9d4.py` | Phase 9D.4 deterministic prompt/policy microbench |
| `run_load_resilience_9d5.py` | Mocked API load/auth resilience check |
| `run_final_backend_acceptance_9d6.py` | Phase 9D.6 acceptance stages |
| `audit_grounded_answers.py` / `write_grounding_audit_report.py` | Grounding audits |
| `run_grounding_regression.py` | Bounded paid grounding regression |
| `run_retrieval_eval.py` | Retrieval coverage eval |

Evaluation uses the same `run_agent` path and unified verification as production.

## 4. Database administration

| Script | Purpose |
| --- | --- |
| `run_migrate.py` | Alembic helper |
| `check_database.py` | Connectivity / basic schema checks |
| `import_seed_sources.py` / `import_registry_config.py` | Registry imports |
| `run_corpus_reliability.py` | Corpus backup/manifest helpers |

## 5. Coverage / inventory analytics

Offline corpus readiness analysis (does not change answer behavior):

`run_coverage_audit.py`, `run_pathway_inventory.py`, `run_pathway_normalization.py`,
`run_evidence_validation.py`, `run_evidence_remediation.py`, `run_source_expansion.py`,
`run_source_gap_closure.py`, and related discover/fetch/compare helpers.

## Related docs

- [`../docs/README.md`](../docs/README.md)
- [`../docs/ops/production.md`](../docs/ops/production.md)
- [`../docs/maintenance/development_history.md`](../docs/maintenance/development_history.md)
