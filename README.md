# LifePilot

AI-powered personal life administration platform. It acts as an intelligent case manager for complex life transitions—starting with international relocation, immigration admin, and employment onboarding in the UK.

## Current status

Phases completed so far:

1. **Local Postgres** — Docker Compose with PostgreSQL 17 + pgvector; connection smoke test
2. **Knowledge schema** — SQLAlchemy 2.x models + Alembic migration for government knowledge collection
3. **Source registry** — curated official seed URLs (GB, DE, NL, CA, EU), idempotent importer, HTTP reachability checks, explicit approval workflow
4. **Dry-run crawl planner** — allowlist + robots.txt + rate-policy plan for approved seeds
5. **Seed HTML fetcher** — fetch approved seed URLs only; dry-run by default
6. **Bounded link discovery** — extract in-allowlist links from stored HTML; fetch in-scope HTML (PDFs listed here; binary download is a separate command)
7. **HTML content-quality checks** — reject recognizable bot-protection / WAF interstitials (e.g. Radware) before storing versions
8. **PDF/DOCX download** — discover in-allowlist binary links from stored HTML; validate magic/OOXML; versioned raw storage under `data/raw/`
9. **Structured HTML extraction** — convert stored HTML versions into ordered JSON blocks (title/headings/paragraphs/lists/tables); raw files unchanged
10. **Structured PDF/DOCX extraction** — extract searchable text from stored binaries (PDF page provenance; DOCX style hierarchy); no OCR
11. **Meaningful content comparison** — compare successful extractions across versions (`unchanged` / `changed` / `unavailable`); ignore raw-only diffs; persist structured diffs
12. **Unified ingestion pipeline** — dry-run/execute orchestration of approved-source fetch → discovery → extract → compare with global safety caps and resumable reruns

Not yet implemented: open crawl, OCR, chunking/embeddings/RAG, API, frontend, or scheduling.

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker Engine + Compose)
- Python 3.11+ recommended
- Git

## Setup from scratch

### 1. Clone and enter the repo

```bash
git clone <your-repo-url>
cd LifePilot
```

### 2. Configure environment

```bash
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

If the password contains special characters (e.g. `!`, `@`, `#`), URL-encode them in `DATABASE_URL` (e.g. `!` → `%21`). Unencoded `!` can cause connection hangs.

### 3. Create a virtual environment and install deps

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

### 4. Start PostgreSQL

```bash
docker compose up -d
docker compose ps
```

### 5. Verify the database connection

```bash
python check_db.py
```

Expected:

```text
PostgreSQL connection successful: 1
```

### 6. Apply database migrations

```powershell
# Windows PowerShell
$env:PYTHONPATH = "backend"
alembic upgrade head
```

```bash
# macOS / Linux
export PYTHONPATH=backend
alembic upgrade head
```

Or: `python scripts/run_migrate.py`

### 7. Import official seed sources

```powershell
$env:PYTHONPATH = "backend"
python scripts/import_seed_sources.py
```

This upserts jurisdictions, authorities, and sources. It is safe to run repeatedly (idempotent).

- New sources are created as `candidate`
- HTTP reachability is reported only — it does **not** approve or downgrade sources
- Existing `review_status` values are preserved on re-import

Explicit approval (human review):

```powershell
python scripts/import_seed_sources.py --no-verify --set-status gb_skilled_worker_visa=approved
```

Allowed statuses: `approved`, `candidate`, `rejected`. Approving requires an HTTPS seed URL and valid allowlist patterns.

### 8. Dry-run crawl plan (approved sources only)

```powershell
$env:PYTHONPATH = "backend"
python scripts/plan_crawl.py
```

This checks each **approved** source seed against its allowlist and `robots.txt`, then prints what a future fetcher would do. It does **not** download government pages or write `documents` rows (it may fetch `/robots.txt` only).

### 9. Fetch approved seed HTML

Dry-run (default — preflight only, no page download):

```powershell
python scripts/fetch_seeds.py
python scripts/fetch_seeds.py --source-id 86
```

Persist HTML + `documents` / `document_versions` (explicit opt-in; commits per seed):

```powershell
python scripts/fetch_seeds.py --persist --source-id 86
python scripts/fetch_seeds.py --persist
```

Files are stored under `data/raw/documents/{document_id}/v{n}.html` (gitignored). Seed fetch accepts HTML only (bot-protection pages are not stored).

### 10. Bounded HTML link discovery (from stored HTML)

Requires a previously persisted seed document. Dry-run (default — list only):

```powershell
python scripts/discover_links.py --source-id 86
python scripts/discover_links.py --document-id 99
```

Persist in-scope HTML links (capped per run; PDFs/DOCX are listed but not downloaded here):

```powershell
python scripts/discover_links.py --source-id 86 --persist
```

Discovery stays inside each source’s allowlist. It does not spider the open web. Host pacing honors robots.txt `Crawl-delay` (or the policy default).

### 11. PDF/DOCX download (from stored HTML)

```powershell
python scripts/discover_documents.py --source-id N
python scripts/discover_documents.py --source-id N --persist
python scripts/discover_documents.py --source-id N --persist --max-documents 3
```

Downloads in-allowlist PDF/DOCX only (magic/OOXML validated). Default cap is 3 binaries per run. Stored as `data/raw/documents/{document_id}/v{n}.pdf` or `.docx`.

### 12. Structured extraction (from stored versions)

Primary CLI: `scripts/extract_documents.py` (HTML/PDF/DOCX). `scripts/extract_html.py` remains a thin HTML-only compatibility wrapper.

Reads existing raw files only — no crawl or download. Dry-run (default):

```powershell
python scripts/extract_documents.py --document-id 99
python scripts/extract_documents.py --pdf --document-id 333
python scripts/extract_documents.py --kinds html,pdf,docx --max-documents 5
python scripts/extract_html.py --document-id 99
```

Persist derived JSON + `document_extractions` (explicit opt-in; commits per document version):

```powershell
python scripts/extract_documents.py --persist --pdf --max-documents 3
python scripts/extract_html.py --persist --document-id 99
```

Output lives under `data/extracted/documents/{document_id}/v{n}.{extractor}.json` (gitignored). Documents with `discovery_status=rejected` are skipped. Raw `data/raw/` files are never rewritten. Re-running the same extractor version with the same content hash is a no-op (`unchanged`); bumping an extractor version updates the current extraction row for that `(document_version_id, extractor_name)`.

### 13. Meaningful content comparison (from stored extractions)

Compares successful compatible extractions across document versions. Does **not** crawl, download, or extract. Dry-run (default):

```powershell
python scripts/compare_documents.py --document-id 99
python scripts/compare_documents.py --previous-version-id A --current-version-id B
python scripts/compare_documents.py --max-documents 10
```

Persist comparison JSON + `document_content_comparisons` (explicit opt-in):

```powershell
python scripts/compare_documents.py --persist --document-id 99
```

Statuses: `unchanged` (normalized extracted content identical), `changed` (textual differences), `unavailable` (missing/failed/incompatible extractions — with an explicit reason). Raw SHA-256 differences alone do not imply a meaningful change. Output lives under `data/compared/documents/...` when persisted.

### 14. Unified ingestion pipeline

One command orchestrates approved-source seed fetch, bounded HTML/PDF/DOCX discovery, structured extraction, and consecutive-version comparison. It reuses the existing crawl/extract/compare modules (no second crawling system).

Modes:
- `full` (default) — fetch/discover + extract/compare
- `process-existing` — extract/compare stored raw versions only (**zero HTTP**, including robots.txt)

Dry-run (default — **no network**, no DB/filesystem writes):

```powershell
$env:PYTHONPATH = "backend"
python scripts/run_ingestion.py --source-id 86
python scripts/run_ingestion.py --max-sources 1 --json
python scripts/run_ingestion.py --mode process-existing --all-approved --json
```

Execute (explicit opt-in; conservatively bounded):

```powershell
python scripts/run_ingestion.py --source-id 86 --execute
python scripts/run_ingestion.py --source-id 86 --execute --max-fetches 5 --max-extractions 10 --max-comparisons 10
python scripts/run_ingestion.py --mode process-existing --all-approved --execute --max-extractions 20 --max-comparisons 20
python scripts/run_ingestion.py --all-approved --execute --max-fetches 5
```

Source selection: one `--source-id`, a bounded `--max-sources` set, or `--all-approved` (deterministic `id` order). Candidate/unapproved sources are never selected.

Default limits: `max_sources=1`, `max_fetches=5`, `max_html_per_source=3`, `max_binary_per_source=2`, `max_extractions=10`, `max_comparisons=10`. Global fetch/extract/compare budgets apply across the whole run (not reset per source). Robots.txt preflight is outside the document-fetch budget.

Resumability: identical raw content does not create duplicate versions; compatible successful extractions are skipped; comparison persistence stays idempotent. Interrupted runs and extraction backlogs can be re-executed safely (`process-existing` until backlog is exhausted). Execute mode writes a JSON summary under `data/runs/` (gitignored) with per-source counts, backlogs, and limit flags.

Does not widen allowlists, bypass bot protection, approve sources, or crawl unbounded.

### 15. Run tests

```powershell
$env:PYTHONPATH = "backend"
pytest -v
```

## Useful commands

| Command | Purpose |
|---|---|
| `docker compose up -d` | Start Postgres in the background |
| `docker compose down` | Stop containers (keeps data volume) |
| `docker compose down -v` | Stop and **delete** the Postgres data volume |
| `docker compose logs -f postgres` | Tail Postgres logs |
| `python check_db.py` | Smoke-test SQLAlchemy → Postgres |
| `alembic upgrade head` | Apply migrations (`PYTHONPATH=backend`) |
| `alembic current` | Show current migration revision |
| `python scripts/import_seed_sources.py` | Upsert curated official seed sources |
| `python scripts/plan_crawl.py` | Dry-run crawl plan for approved sources |
| `python scripts/fetch_seeds.py` | Dry-run seed fetch preflight |
| `python scripts/fetch_seeds.py --persist` | Download/store approved seed HTML |
| `python scripts/discover_links.py --source-id N` | Dry-run HTML link discovery from stored HTML |
| `python scripts/discover_links.py --source-id N --persist` | Fetch in-scope discovered HTML |
| `python scripts/mark_document_discovery_status.py` | Explicitly exclude/include a document for discovery |
| `python scripts/set_source_patterns.py` | Explicit allowlist change (requires `--confirm-expand` to widen) |
| `python scripts/discover_documents.py --source-id N` | Dry-run PDF/DOCX discovery from stored HTML |
| `python scripts/discover_documents.py --source-id N --persist` | Download/store in-scope PDF/DOCX (default max 3) |
| `python scripts/extract_documents.py` | Dry-run structured extraction (HTML/PDF/DOCX) |
| `python scripts/extract_documents.py --persist --pdf --document-id N` | Persist extraction JSON + DB row |
| `python scripts/extract_html.py` | Thin HTML-only wrapper around extract_documents |
| `python scripts/compare_documents.py --document-id N` | Dry-run meaningful content comparison |
| `python scripts/compare_documents.py --persist --document-id N` | Persist comparison JSON + DB row |
| `python scripts/run_ingestion.py --source-id N` | Dry-run unified ingestion plan |
| `python scripts/run_ingestion.py --source-id N --execute` | Bounded fetch/extract/compare run |
| `python scripts/run_ingestion.py --mode process-existing --all-approved --execute` | Drain extraction/compare backlog (no HTTP) |
| `python scripts/run_ingestion.py --all-approved --execute` | Multi-source full ingest under global budgets |
| `python scripts/run_ingestion.py --country SG` | Dry-run approved sources for one jurisdiction/country |
| `python scripts/import_registry_config.py --config config/registry/global_candidates_6c2.json` | Import candidate countries/sources + approval manifest |
| `pytest -v` | Run schema + registry + crawl + extract tests |

### Inspect tables in Postgres

```bash
docker compose exec -T postgres psql -U lifepilot -d lifepilot
```

```sql
\dt
SELECT id, name, seed_url, review_status FROM sources ORDER BY id;
\d documents
\d document_versions
```

## Project layout (so far)

```text
LifePilot/
├── compose.yaml                 # Postgres + pgvector
├── .env.example                 # Env template (safe to commit)
├── .env                         # Local secrets (gitignored)
├── check_db.py                  # DB connectivity smoke test
├── requirements.txt
├── alembic.ini
├── alembic/                     # Migrations
├── backend/lifepilot/
│   ├── db/                      # SQLAlchemy models + session
│   ├── registry/                # Seed registry (import, verify, review, URL scope)
│   ├── crawl/                   # Planner, seed fetch, bounded link/binary discovery
│   ├── extract/                 # Structured HTML/PDF/DOCX extraction + comparison
│   └── ingest/                  # Unified bounded ingestion orchestration
├── data/                        # Raw + extracted + compared + run reports (gitignored)
├── tests/                       # Schema + registry + crawl + extract + ingest tests
├── scripts/
│   ├── run_migrate.py
│   ├── import_seed_sources.py
│   ├── plan_crawl.py
│   ├── fetch_seeds.py
│   ├── discover_links.py
│   ├── mark_document_discovery_status.py
│   ├── set_source_patterns.py
│   ├── discover_documents.py
│   ├── extract_html.py
│   ├── extract_documents.py
│   ├── compare_documents.py
│   └── run_ingestion.py
├── pytest.ini
├── .gitignore
└── README.md
```

## Knowledge schema (overview)

| Table | Purpose |
|---|---|
| `jurisdictions` | Countries, regions, municipalities, EU (supranational), etc. |
| `authorities` | Publishing government bodies |
| `sources` | Curated seed URLs + explicit `review_status` |
| `documents` | One row per normalized URL / language variant |
| `document_versions` | Historical raw snapshots; at most one `is_current` per document |
| `document_extractions` | Derived structured content per version + extractor (raw files immutable) |
| `document_content_comparisons` | Meaningful content diffs between two versions (idempotent; no silent overwrite) |
| `document_jurisdictions` | Explicit applicability (never inferred from hierarchy alone) |

## Source registry / crawl safety notes

- Seed metadata is separate from downloaded documents
- Allowlist patterns use a narrow path scope (`https://host/path*`); matching requires exact scheme/host and genuine path descendants — not naive string prefixes
- HTTP verification and fetching use a transparent LifePilot user-agent (`LifePilotFetcher` for HTML/PDF/DOCX) and do not impersonate a browser
- Successful HTTP checks are **not** content authenticity; `robots.txt`, terms, and site policies still apply
- Host pacing uses robots.txt `Crawl-delay` when present (otherwise `default_crawl_delay_seconds`, floored by `min_crawl_delay_seconds`). `max_requests_per_host_per_minute` is advisory for planning/reporting only — not enforced as a hard fetcher limit
- Per-run caps: discovered HTML fetches, discovered binary fetches, max response bytes, max redirects, blocked private/loopback hosts
- Link discovery is bounded: only in-allowlist URLs from stored HTML; HTML fetch via `discover_links.py`, PDF/DOCX via `discover_documents.py`
- Fetched HTML that looks like a bot-protection interstitial is not stored as a successful version; sources stay approved
- Storage path reads use resolved-path containment (`Path.is_relative_to`); batch persist commits per document/version
- Approved allowlists do not widen on seed re-import; use `scripts/set_source_patterns.py --confirm-expand` for intentional expansions

## Stack (planned)

| Layer | Choice |
|---|---|
| Frontend | Next.js, React, TypeScript, Tailwind |
| Backend | Python, FastAPI, Pydantic |
| Database | PostgreSQL, SQLAlchemy, Alembic, pgvector |
| AI | LangGraph/LangChain, OpenAI and/or Anthropic |
| Infra | Docker, GitHub Actions |

## Next steps

Phase 6C multi-source backlog processing (`process-existing`, `--all-approved`), then chunking/embeddings/RAG and API/frontend—built incrementally. Do not wipe historical raw/extraction/comparison versions unless explicitly intended.
