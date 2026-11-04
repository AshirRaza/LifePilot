# LifePilot

LifePilot is an AI-powered personal administration platform intended to act as an intelligent case manager for complex life transitions—starting with international relocation, immigration administration, and employment onboarding.

Today the repository is a **knowledge-base foundation**: curated official government sources are authorized, fetched, versioned, extracted, and assessed for immigration-pathway coverage. The goal is **evidence-backed immigration information** grounded in official documents—not chatbot answers.

| Evidence class | Meaning |
|---|---|
| Verified official evidence | Ingested from approved exact URLs; passages assessed against critical requirements |
| Provisional readiness | Automated quality gate: all critical cells COMPLETE with passage/doc quality checks |
| Human-verified readiness | Explicit human review only (`confirm-pathway`); **never** auto-granted |
| Future AI guidance | Planned (Phases 8–11); not present in this codebase |

Provisional readiness is **not** legal advice, human verification, or permission to rely on the corpus without checking current official requirements.

## Current architecture

Implemented stack (no FastAPI app, no frontend, no RAG yet):

| Component | Role |
|---|---|
| Python 3.11+ | Application and CLI tooling |
| PostgreSQL 17 + Docker Compose | Durable registry, documents, versions, frontier |
| SQLAlchemy 2.x + Alembic | Models and migrations |
| Official source registry | Seed import, HTTPS allowlists, explicit approval |
| Exact-URL authorization | Fetches stay inside approved patterns; redirects do not widen allowlists |
| Document ingestion & versioning | HTML/PDF/DOCX fetch, immutable raw bytes, structured extraction, content comparison |
| Evidence extraction & validation | Critical-requirement assessments, contamination/wrong-pathway rejection |
| Pathway coverage & readiness | 102 pathways × 5 requirements; provisional vs human-verified readiness |
| Automated tests | pytest suite covering schema, registry, crawl, extract, coverage |

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
│   └── coverage/                # Pathway inventory, evidence, corpus reliability
│       ├── normalization/
│       ├── evidence_validation/
│       ├── evidence_remediation/
│       ├── source_gap_closure/
│       ├── pathway_inventory/
│       ├── source_expansion/
│       └── corpus_reliability/  # Backup, audit, readiness, human review CLI
├── scripts/                     # Operational CLIs (see below)
├── tests/
├── data/                        # Local raw/extracted/compared/runs (gitignored)
└── backups/                     # Local DB + data backups (gitignored)
```

## Current project status — Phase 6U

Snapshot dated **2026-11-04** (UTC), verified against `data/runs/coverage_matrix.csv`, `data/runs/corpus_status_report.md`, and the live Postgres corpus.

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
| Automated tests (current suite) | 386 passed, 2 skipped |

Corpus metrics match the Phase 6U report and live DB. The Phase 6U report also recorded **469 passed, 2 skipped** before one-shot phase runners/tests were retired; the current suite count above is authoritative for this tree.

Provisionally ready pathways (quality-gated, automated only):

`gb_skilled_worker`, `gb_student`, `ie_critical_skills_employment_permit`, `ie_general_employment_permit`, `nl_highly_skilled_migrant`, `nl_naturalisation`, `sg_employment_pass`

Phases 1–6U delivered the knowledge-base foundation through ingestion, provenance, evidence quality gates, and corpus reliability tooling. One-shot phase runners were retired after 6U; durable state lives in the database and in `data/runs/evidence_matrix.csv` / `coverage_matrix.csv`.

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

DATABASE_URL=postgresql+psycopg://lifepilot:your_password_here@localhost:5432/lifepilot
```

If the password contains special characters (e.g. `!`, `@`, `#`), URL-encode them in `DATABASE_URL` (e.g. `!` → `%21`).

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
| `scripts/extract_documents.py` | Structured extraction |
| `scripts/compare_documents.py` | Meaningful content comparison |
| `scripts/set_source_patterns.py` | Explicit allowlist change (`--confirm-expand` to widen) |

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

### 7. Corpus reliability CLI

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

### 8. Run tests

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
- **Operational caution:** Always verify current official government requirements before relying on any immigration guidance derived from this corpus.

## Development roadmap

| Phase | Scope | Status |
|---|---|---|
| Phases 1–6U | Knowledge-base foundation, ingestion, provenance, evidence quality | Implemented to current scope |
| Phase 7 | Six external knowledge source integrations | Planned |
| Phase 8 | Internal RAG with embeddings and pgvector | Planned |
| Phase 9 | AI agent combining internal and external evidence | Planned |
| Phase 10 | FastAPI backend | Planned |
| Phase 11 | Frontend and MVP | Planned |

### Phase 7 — planned integrations (not operational)

| Source | Notes |
|---|---|
| GOV.UK Content API | Public API |
| Canada Open Government | Public open-data APIs |
| EU Open Data Portal | Public open-data APIs |
| EUR-Lex / CELLAR | Public legal API |
| US Federal Register | Public API |
| EU Immigration Portal | Website; supported programmatic access not yet confirmed |

Do not treat these as live connectors. Public APIs still require design for authorization, caching, provenance, and rate limits before use.

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
| `pytest -v` | Full test suite |

### Inspect tables

```bash
docker compose exec -T postgres psql -U lifepilot -d lifepilot
```

```sql
\dt
SELECT id, name, seed_url, review_status FROM sources ORDER BY id;
```

## Storage persistence

- **Raw document bytes** live under `data/raw/` (gitignored).
- **Postgres** uses the Docker named volume `postgres_data`.
- Backups must include **both** the database dump and the `data/` tree (`scripts/run_corpus_reliability.py backup`).

Do not wipe historical raw/extraction/comparison versions unless explicitly intended. Do not treat a government URL alone as proof of a complete immigration requirement.
