# LifePilot scripts

Prefer `PYTHONPATH=backend` from the repo root. Evaluation adapters that force
`external_only` are diagnostic-only.

Compatibility shims remain at a few historical top-level paths
(`run_api.py`, `run_knowledge_maintenance.py`, `ask_lifepilot.py`,
`check_database.py`, `import_seed_sources.py`). Prefer the organized paths below.

## operations/

| Script | Purpose |
| --- | --- |
| `ask_lifepilot.py` | Full agent ask |
| `run_api.py` | Private FastAPI server |
| `run_agent_evidence.py` | Evidence orchestration only |
| `query_internal_knowledge.py` | Internal hybrid RAG |
| `query_external_knowledge.py` | Official external connectors |
| `run_national_official_smoke.py` | NL/IE/FR/SG connector smoke |
| `run_external_live_smoke.py` | Live external smoke (optional) |

## maintenance/

| Script | Purpose |
| --- | --- |
| `run_knowledge_maintenance.py` | Bounded KB recheck / classify / queue |
| `check_source_freshness.py` | Freshness ages |
| `run_ingestion.py` / `run_chunk_documents.py` / `run_embed_chunks.py` | Corpus build |
| `fetch_seeds.py`, `extract_documents.py`, `compare_documents.py`, … | Crawl/extract helpers |

## database/

| Script | Purpose |
| --- | --- |
| `check_database.py` | Connectivity / schema checks |
| `run_migrate.py` | Alembic helper |
| `import_seed_sources.py` / `import_registry_config.py` | Registry imports |
| `run_corpus_reliability.py` | Corpus backup/manifest helpers |

## evaluation/

| Script | Purpose |
| --- | --- |
| `run_grounded_qa_eval.py` | 50-question grounded QA eval |
| `run_comparative_answer_eval.py` | Internal / external / hybrid comparison |
| `run_unified_verification_eval.py` | Targeted verification eval |
| `run_verification_reliability_eval.py` | Claim-level verifier suite |
| `run_verification_reliability_e2e.py` | Bounded live agent E2E |
| `build_verification_reliability_fixtures.py` | Rebuild reliability fixtures |
| `run_performance_benchmark.py` / `run_performance_microbench.py` | Latency/cost benches |
| `run_api_load_resilience.py` | Mocked API load/auth resilience |
| `run_grounding_regression.py` / `run_retrieval_eval.py` | Grounding / retrieval eval |

## corpus/

Offline corpus readiness analytics (does not change answer behavior):
`run_coverage_audit.py`, `run_pathway_inventory.py`, `run_pathway_normalization.py`,
`run_evidence_validation.py`, `run_evidence_remediation.py`, `run_source_expansion.py`,
`run_source_gap_closure.py`.

## Docs

- [`../docs/README.md`](../docs/README.md)
- [`../docs/ops/production.md`](../docs/ops/production.md)
