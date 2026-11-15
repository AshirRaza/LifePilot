# LifePilot scripts

Operational and evaluation CLIs. Prefer `PYTHONPATH=backend` (or activate `.venv`
from the repo root). Do not treat evaluation-only adapters as production modes.

## Operational (production-adjacent)

| Script | Purpose |
|---|---|
| `ask_lifepilot.py` | Full agent ask (hybrid retrieval + grounded answer + verification) |
| `run_agent_evidence.py` | Evidence orchestration only (no LLM required) |
| `query_internal_knowledge.py` | Internal hybrid RAG |
| `query_external_knowledge.py` | Official external connectors (`unverified_external`) |
| `run_ingestion.py` / `run_chunk_documents.py` / `run_embed_chunks.py` | Corpus build |
| `run_knowledge_maintenance.py` | Bounded KB recheck / pending changes |
| `run_source_freshness_check.py` | Freshness ages from `DocumentVersion` |
| `run_national_official_smoke.py` | NL/IE/FR/SG connector smoke |
| `run_migrate.py` / `check_database.py` / `import_*.py` | DB / registry ops |

## Evaluation / regression

| Script | Purpose |
|---|---|
| `run_grounded_qa_eval.py` | Original 50-question grounded QA eval |
| `run_comparative_answer_eval.py` | Phase 9D.1 three-mode comparison |
| `run_unified_verification_eval.py` | Phase 9D.2 targeted verification eval |
| `audit_grounded_answers.py` / `write_grounding_audit_report.py` | Grounding audits |
| `run_grounding_regression.py` | Bounded paid grounding regression |
| `run_retrieval_eval.py` | Retrieval coverage eval |

Evaluation uses the **same** `run_agent` path and unified verification as production.
`external_only` in comparative eval is diagnostic-only.

## Coverage / inventory (offline analytics)

`run_coverage_audit.py`, `run_pathway_inventory.py`, `run_corpus_reliability.py`,
`run_evidence_*.py`, `run_source_*.py`, and related inventory scripts support
corpus readiness analysis — they do not change agent answer behavior.
