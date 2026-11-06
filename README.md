# LifePilot

LifePilot is an AI-powered personal administration platform intended to act as an intelligent case manager for complex life transitions—starting with international relocation, immigration administration, and employment onboarding.

Today the repository is a **knowledge-base foundation**: curated official government sources are authorized, fetched, versioned, extracted, assessed for immigration-pathway coverage, chunked, and embedded locally for future retrieval. Read-only connectors can also query selected public government APIs. The goal is **evidence-backed immigration information** grounded in official documents—not chatbot answers.

| Evidence class | Meaning |
|---|---|
| Verified official evidence | Ingested from approved exact URLs; passages assessed against critical requirements |
| Provisional readiness | Automated quality gate: all critical cells COMPLETE with passage/doc quality checks |
| Human-verified readiness | Explicit human review only (`confirm-pathway`); **never** auto-granted |
| External API results | Always `unverified_external`; never auto-promoted into verified pathway evidence |
| Future AI guidance | Planned; retrieval/answers not implemented yet |

Provisional readiness is **not** legal advice, human verification, or permission to rely on the corpus without checking current official requirements.

## Current architecture

| Component | Role |
|---|---|
| Python 3.11+ | Application and CLI tooling |
| PostgreSQL 17 + Docker Compose | Durable registry, documents, versions, frontier |
| pgvector 0.8.2 | Local chunk embeddings (`vector(384)`) |
| SQLAlchemy 2.x + Alembic | Models and migrations |
| Official source registry | Seed import, HTTPS allowlists, explicit approval |
| Exact-URL authorization | Fetches stay inside approved patterns; redirects do not widen allowlists |
| Document ingestion & versioning | HTML/PDF/DOCX fetch, immutable raw bytes, structured extraction, content comparison |
| Evidence extraction & validation | Critical-requirement assessments, contamination/wrong-pathway rejection |
| Pathway coverage & readiness | 102 pathways × 5 requirements; provisional vs human-verified readiness |
| Local RAG prep | Cleaning/chunking + `intfloat/multilingual-e5-small` embeddings (no retrieval yet) |
| External knowledge connectors | Read-only public APIs (GOV.UK, Canada Open, data.europa.eu, CELLAR, Federal Register; EU Immigration Portal gated) |
| Automated tests | pytest suite covering schema, registry, crawl, extract, coverage, RAG, external |

### Directory structure

```text
LifePilot/
├── compose.yaml                 # Postgres + pgvector
├── .env.example                 # Env template (safe to commit)
├── check_db.py                  # DB connectivity smoke test
├── requirements.txt
├── alembic.ini / alembic/       # Migrations
├── config/
│   ├── coverage/                # Pathway seeds, signals, normalization
│   └── registry/                # Candidate source manifests
├── backend/lifepilot/
│   ├── db/                      # SQLAlchemy models + session
│   ├── registry/                # Source registry, URL scope, review
│   ├── crawl/                   # Planner, frontier, seed/link/binary discovery
│   ├── extract/                 # HTML/PDF/DOCX extraction + comparison
│   ├── ingest/                  # Bounded unified ingestion orchestration
│   ├── coverage/                # Pathway inventory, evidence, corpus reliability
│   ├── rag/                     # Clean, chunk, local embed (no retrieval)
│   └── external/                # External API connectors + router
├── scripts/                     # Operational CLIs (see below)
├── tests/
├── data/                        # Local raw/extracted/compared/runs (gitignored)
└── backups/                     # Local DB + data backups (gitignored)
```

## Current project status

Corpus snapshot dated **2026-11-04** (UTC), verified against `data/runs/coverage_matrix.csv` and the live Postgres corpus. RAG embedding counts verified **2026-11-06**. Test counts from the current suite (**2026-11-06**).

| Metric | Value |
|---|---:|
| Jurisdictions | 13 |
| Immigration pathways | 102 |
| Documents | 601 |
| Approved sources | 63 |
| Document versions | 670 |
| Coverage cells | 510 |
| COMPLETE cells | 65 |
| PARTIAL cells | 62 |
| MISSING cells | 94 |
| UNEVALUATED cells | 286 |
| NOT_APPLICABLE cells | 3 |
| Provisionally ready pathways | 7 |
| Human-verified pathways | 0 |
| Current document chunks | 3682 |
| Chunk embeddings (E5-small / 384-d) | 3682 |
| Automated tests | 434 passed, 3 skipped |

Provisionally ready pathways (quality-gated, automated only):

`gb_skilled_worker`, `gb_student`, `ie_critical_skills_employment_permit`, `ie_general_employment_permit`, `nl_highly_skilled_migrant`, `nl_naturalisation`, `sg_employment_pass`

Durable coverage state lives in the database and in `data/runs/evidence_matrix.csv` / `coverage_matrix.csv`.

## Setup and usage

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker Engine + Compose)
- Python 3.11+
- Git

### 1. Clone and configure environment

```bash
git clone <your-repo-url>
cd LifePilot
cp .env.example .env
```

Edit `.env` and set a strong `POSTGRES_PASSWORD`. Use the **same** password in `DATABASE_URL`:

```env
POSTGRES_DB=lifepilot
POSTGRES_USER=lifepilot
POSTGRES_PASSWORD=your_password_here
POSTGRES_PORT=5432

DATABASE_URL=postgresql+psycopg://lifepilot:your_password_here@127.0.0.1:5432/lifepilot
```

If the password contains special characters (e.g. `!`, `@`, `#`), URL-encode them in `DATABASE_URL` (e.g. `!` → `%21`). Prefer `127.0.0.1` over `localhost` on Windows (IPv6/`::1` resolution can delay or hang connections to Docker Postgres).

### 2. Virtual environment and dependencies

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux:**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Embedding generation downloads `intfloat/multilingual-e5-small` into the local Hugging Face cache (`~/.cache/huggingface/hub`). No paid embedding APIs are used.

### 3. Start PostgreSQL

```bash
docker compose up -d
docker compose ps
```

### 4. Verify connection and apply migrations

```bash
python check_db.py
```

Expected: `PostgreSQL connection successful: 1`

```powershell
# Windows PowerShell
$env:PYTHONPATH = "backend"
alembic upgrade head
# or: python scripts/run_migrate.py
```

```bash
# macOS / Linux
export PYTHONPATH=backend
alembic upgrade head
```

Optional isolated test database (see comments in `.env.example`):

```bash
docker compose exec postgres psql -U lifepilot -d postgres -c "CREATE DATABASE lifepilot_test"
```

### 5. Import curated seed sources

```powershell
$env:PYTHONPATH = "backend"
python scripts/import_seed_sources.py
```

Idempotent upsert. New sources start as `candidate`. HTTP checks do **not** approve sources. Explicit approval example:

```powershell
python scripts/import_seed_sources.py --no-verify --set-status gb_skilled_worker_visa=approved
```

### 6. Ingestion (dry-run by default)

Unified pipeline (approved sources only; bounded caps):

```powershell
$env:PYTHONPATH = "backend"
python scripts/run_ingestion.py --source-id 86
python scripts/run_ingestion.py --source-id 86 --execute
python scripts/run_ingestion.py --mode process-existing --all-approved --execute
```

Supporting CLIs (also dry-run unless `--persist` / `--execute`):

| Script | Purpose |
|---|---|
| `scripts/plan_crawl.py` | Dry-run crawl plan for approved sources |
| `scripts/fetch_seeds.py` | Seed HTML fetch |
| `scripts/discover_links.py` | Bounded HTML link discovery |
| `scripts/discover_documents.py` | PDF/DOCX discovery/download |
| `scripts/extract_documents.py` | Structured HTML/PDF/DOCX extraction |
| `scripts/compare_documents.py` | Meaningful content comparison |
| `scripts/set_source_patterns.py` | Explicit allowlist change (`--confirm-expand` to widen) |
| `scripts/mark_document_discovery_status.py` | Manually set `documents.discovery_status` |

Coverage pipeline (inventory → validation → remediation → expansion):

```powershell
$env:PYTHONPATH = "backend"
python scripts/run_coverage_audit.py
python scripts/run_pathway_normalization.py
python scripts/run_evidence_validation.py
python scripts/run_evidence_remediation.py
python scripts/run_source_gap_closure.py
python scripts/run_pathway_inventory.py
python scripts/run_source_expansion.py
```

### 7. Chunking and local embeddings

Idempotent. Does not change evidence classifications or source approvals.

```powershell
$env:PYTHONPATH = "backend"
python scripts/run_chunk_documents.py
python scripts/run_embed_chunks.py
```

Optional: `--force` to rewrite; `--cuda` for GPU when available (CPU is default).

### 8. Corpus reliability CLI

```powershell
$env:PYTHONPATH = "backend"
python scripts/run_corpus_reliability.py status
python scripts/run_corpus_reliability.py status --with-db
python scripts/run_corpus_reliability.py backup
python scripts/run_corpus_reliability.py readiness-audit
python scripts/run_corpus_reliability.py consistency-audit
python scripts/run_corpus_reliability.py discovery
```

Human review (never automatic):

```powershell
python scripts/run_corpus_reliability.py review --pathway-id PATH --requirement eligibility --decision confirm --reviewer YOU
python scripts/run_corpus_reliability.py confirm-pathway --pathway-id PATH --reviewer YOU
```

### 9. External knowledge retrieval

Read-only public API discovery. Does **not** ingest documents, widen allowlists, or change coverage readiness. Results are always `unverified_external`.

```powershell
$env:PYTHONPATH = "backend"
python scripts/query_external_knowledge.py --status
python scripts/query_external_knowledge.py --pathway-id gb_skilled_worker --jurisdiction GB
python scripts/query_external_knowledge.py --query immigration --jurisdiction CA --provider canada_open --json
python scripts/run_external_live_smoke.py
```

Optional live pytest smoke (off by default):

```powershell
$env:LIFEPILOT_LIVE_EXTERNAL = "1"
pytest tests/test_external_knowledge.py::test_live_govuk_smoke -q
```

### 10. Run tests

```powershell
$env:PYTHONPATH = "backend"
pytest -v
```

## Evidence quality and safety

- **Authorization:** Only HTTPS sources with explicit `approved` status and path-scoped allowlist patterns are fetchable. Exact-URL controls apply; redirects must not silently widen allowlists.
- **Provenance & versioning:** Each document URL has versioned raw snapshots (`document_versions`); extractions and comparisons are derived and do not rewrite raw bytes.
- **Quality gates:** Heading-only or navigational passages cannot mark a critical requirement COMPLETE. Cross-jurisdiction and wrong-pathway associations are rejected.
- **Contamination controls:** Blocklists and applicability checks prevent off-purpose forms or foreign-jurisdiction pages from completing a cell.
- **Readiness:** `pathway_is_provisionally_ready` is automated and quality-gated. Human-verified readiness requires an explicit `confirm-pathway` decision. **0 pathways are human-verified.**
- **External APIs:** Successful HTTP responses are not verified pathway evidence and do not change COMPLETE/PARTIAL/MISSING classifications.
- **RAG prep:** Chunking/embedding never reclassify evidence or approve sources. Embeddings are local only.
- **Operational caution:** Always verify current official government requirements before relying on any immigration guidance derived from this corpus.

## External connectors

| Source | Status |
|---|---|
| GOV.UK Content API | Working (public JSON by path) |
| Canada Open Government | Working (CKAN dataset search; not IRCC eligibility) |
| EU Open Data Portal | Working (dataset search metadata) |
| EUR-Lex / CELLAR | Working (SPARQL + CELEX resource; SOAP search needs registration) |
| US Federal Register | Working (documents.json search) |
| EU Immigration Portal | Owner action required — no public API; exact URL approval needed |

## Development roadmap

| Scope | Status |
|---|---|
| Knowledge-base foundation, ingestion, provenance, evidence quality | Implemented |
| External public-API knowledge connectors | Implemented (EU Immigration Portal gated) |
| RAG database foundation (pgvector + chunk schema) | Implemented |
| Document cleaning and chunking | Implemented (`document_chunks` populated) |
| Local chunk embeddings (pgvector) | Implemented |
| Semantic retrieval / RAG answers | Planned (Phase 8E) |
| AI agent combining internal and external evidence | Planned |
| FastAPI backend | Planned |
| Frontend and MVP | Planned |

## Knowledge schema (overview)

| Table | Purpose |
|---|---|
| `jurisdictions` | Countries, regions, municipalities, EU, etc. |
| `authorities` | Publishing government bodies |
| `sources` | Curated seed URLs + explicit `review_status` |
| `documents` | One row per normalized URL / language variant |
| `document_versions` | Historical raw snapshots; at most one `is_current` per document |
| `document_extractions` | Derived structured content per version + extractor |
| `document_content_comparisons` | Meaningful content diffs between versions |
| `document_jurisdictions` | Explicit applicability (never inferred from hierarchy alone) |
| `discovery_frontier` | Resumable bounded crawl frontier |
| `embedding_models` | Embedding model registry (dimensions, notes) |
| `document_chunks` | Current-version text chunks with mandatory jurisdiction |
| `document_chunk_embeddings` | 384-d vectors linked to chunk + model |

## Useful commands

| Command | Purpose |
|---|---|
| `docker compose up -d` | Start Postgres |
| `docker compose down` | Stop containers (keeps volume) |
| `python check_db.py` | Smoke-test DB connection |
| `alembic upgrade head` | Apply migrations (`PYTHONPATH=backend`) |
| `python scripts/import_seed_sources.py` | Upsert curated official seeds |
| `python scripts/run_ingestion.py --source-id N` | Dry-run unified ingestion |
| `python scripts/run_ingestion.py --source-id N --execute` | Bounded execute |
| `python scripts/run_corpus_reliability.py status` | Coverage / readiness summary |
| `python scripts/query_external_knowledge.py --status` | List external providers |
| `python scripts/run_chunk_documents.py` | Chunk current extractions |
| `python scripts/run_embed_chunks.py` | Embed current chunks locally |
| `pytest -v` | Full test suite |

### Inspect tables

```bash
docker compose exec -T postgres psql -U lifepilot -d lifepilot
```

```sql
\dt
SELECT id, name, seed_url, review_status FROM sources ORDER BY id;
SELECT COUNT(*) FROM document_chunks WHERE is_current_version;
SELECT COUNT(*) FROM document_chunk_embeddings;
```

## Storage persistence

- **Raw document bytes** live under `data/raw/` (gitignored).
- **Postgres** uses the Docker named volume `postgres_data`.
- **Embeddings / HF model cache** are local only (DB + `~/.cache/huggingface/hub`).
- Backups must include **both** the database dump and the `data/` tree (`scripts/run_corpus_reliability.py backup`).

Do not wipe historical raw/extraction/comparison versions unless explicitly intended. Do not treat a government URL alone as proof of a complete immigration requirement.
