# LifePilot

AI-powered personal administration for complex life transitions—starting with international relocation and immigration admin.

This repo is an **evidence-backed knowledge foundation**: approved official sources are ingested, versioned, assessed for pathway coverage, chunked, and retrieved via hybrid RAG. A LangGraph agent orchestrates internal evidence (and optional external connectors) and can produce **Claude grounded answers** when Anthropic is configured. Nothing here is legal advice.

| Evidence class | Meaning |
|---|---|
| Verified official evidence | Approved exact URLs; assessed against critical requirements |
| Provisional readiness | Automated quality gate only—not human verification |
| External API results | Always `unverified_external`; never auto-promoted |
| Internal RAG hits | Always `unverified_retrieval`; not pathway COMPLETE |
| Agent answers | Claude grounded answers when Anthropic is configured; otherwise evidence only |

## Stack

Python 3.11+ · PostgreSQL 17 + pgvector · SQLAlchemy/Alembic · local E5 embeddings · LangGraph · pytest

```text
backend/lifepilot/
  db/  registry/  crawl/  extract/  ingest/
  coverage/  rag/  external/  agent/
scripts/   config/   alembic/   tests/   docs/
data/ (gitignored local corpus, runs, backups)
```

## Status (approx.)

| | |
|---|---:|
| Jurisdictions | 14 |
| Pathways | 102 |
| Documents / versions | 601 / 670 |
| Approved sources | ~63–80 |
| Current chunks + embeddings | 9883 |
| Provisionally ready pathways | 7 |
| Human-verified pathways | 0 |
| Tests | 506 passed, 5 skipped |

Provisionally ready: `gb_skilled_worker`, `gb_student`, `ie_critical_skills_employment_permit`, `ie_general_employment_permit`, `nl_highly_skilled_migrant`, `nl_naturalisation`, `sg_employment_pass`.

## Setup

```powershell
cp .env.example .env   # set POSTGRES_PASSWORD + matching DATABASE_URL (prefer 127.0.0.1)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
docker compose up -d
python scripts/check_database.py
$env:PYTHONPATH = "backend"
alembic upgrade head
python scripts/import_seed_sources.py
```

Optional test DB: create `lifepilot_test` (see `.env.example`).

## Common commands

```powershell
$env:PYTHONPATH = "backend"

# Ingestion (dry-run unless --execute)
python scripts/run_ingestion.py --source-id N
python scripts/run_ingestion.py --source-id N --execute

# Chunk + embed (local E5; no paid APIs)
python scripts/run_chunk_documents.py
python scripts/run_embed_chunks.py

# Internal hybrid retrieval (jurisdiction required)
python scripts/query_internal_knowledge.py --query "Skilled Worker salary" --jurisdiction GB --mode hybrid

# External connectors (unverified_external; no corpus writes)
python scripts/query_external_knowledge.py --pathway-id gb_skilled_worker --jurisdiction GB

# Evidence-only orchestration (no LLM key required)
python scripts/run_agent_evidence.py --question "What are the UK Skilled Worker visa requirements?" --jurisdiction GB --pathway gb_skilled_worker

# Grounded answer via Claude (requires ANTHROPIC_API_KEY + LIFEPILOT_LLM_PROVIDER=anthropic)
python scripts/ask_lifepilot.py --question "What are the requirements for a UK Skilled Worker visa?" --jurisdiction GB
python scripts/ask_lifepilot.py --question "…" --jurisdiction GB --include-external --json

# Coverage / readiness
python scripts/run_corpus_reliability.py status --with-db

# Evaluation (offline-friendly; paid runs are budget-gated)
python scripts/run_grounded_qa_eval.py --dry-run
python scripts/audit_grounded_answers.py

# Tests
pytest -q
```

Other CLIs: `plan_crawl`, `fetch_seeds`, `discover_links`, `discover_documents`, `extract_documents`, `compare_documents`, coverage audit/inventory/expansion scripts—see `scripts/`.

Maintenance notes: `docs/maintenance/`.

## Safety

- Only HTTPS `approved` sources with path-scoped allowlists are fetchable.
- Raw bytes are versioned; extractions never rewrite raw storage.
- Heading-only / wrong-pathway / cross-jurisdiction hits cannot mark COMPLETE.
- Human-verified readiness requires explicit `confirm-pathway`.
- External APIs and RAG hits do not change coverage classifications.
- Always check current official government requirements before relying on this corpus.

## Roadmap

| Done | Next |
|---|---|
| Registry, ingest, coverage, RAG hybrid retrieval | Auth + multi-turn checkpointing |
| External connectors | FastAPI + frontend MVP |
| LangGraph evidence agent + Claude grounded answers | Personalized eligibility (out of scope until verified facts) |
| Grounding audit / answer composition hardening | Product API surface (later) |

## Schema (core)

`jurisdictions` · `authorities` · `sources` · `documents` · `document_versions` · `document_extractions` · `document_chunks` · `document_chunk_embeddings` · `discovery_frontier` · `document_jurisdictions` (explicit only)

Backups must include **Postgres + `data/`** (`run_corpus_reliability.py backup`).
