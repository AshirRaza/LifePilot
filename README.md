# LifePilot

AI-powered personal life administration platform. It acts as an intelligent case manager for complex life transitions—starting with international relocation, immigration admin, and employment onboarding in the UK.

## Current status

Phases completed so far:

1. **Local Postgres** — Docker Compose with PostgreSQL 17 + pgvector; connection smoke test
2. **Knowledge schema** — SQLAlchemy 2.x models + Alembic migration for government knowledge collection
3. **Source registry** — curated official seed URLs (GB, DE, NL, CA, EU), idempotent importer, HTTP reachability checks, explicit approval workflow
4. **Dry-run crawl planner** — allowlist + robots.txt + rate-policy plan for approved seeds
5. **Seed HTML fetcher** — fetch approved seed URLs only; dry-run by default
6. **Bounded link discovery** — extract in-allowlist links from stored HTML; fetch HTML only (PDFs listed, not downloaded)

No open crawl, PDF/DOCX ingestion, extraction/RAG, API, or frontend yet.

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

Persist HTML + `documents` / `document_versions` (explicit opt-in):

```powershell
python scripts/fetch_seeds.py --persist --source-id 86
python scripts/fetch_seeds.py --persist
```

Files are stored under `data/raw/documents/{document_id}/v{n}.html` (gitignored). Only HTML is accepted.

### 10. Bounded link discovery (from stored HTML)

Requires a previously persisted seed document. Dry-run (default — list only):

```powershell
python scripts/discover_links.py --source-id 86
python scripts/discover_links.py --document-id 99
```

Persist in-scope HTML links (capped per run; PDFs are reported but not downloaded):

```powershell
python scripts/discover_links.py --source-id 86 --persist
```

Discovery stays inside each source’s allowlist. It does not spider the open web.

### 11. Run tests

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
| `python scripts/discover_links.py --source-id N` | Dry-run link discovery from stored HTML |
| `python scripts/discover_links.py --source-id N --persist` | Fetch in-scope discovered HTML |
| `pytest -v` | Run schema + registry + crawl tests |

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
│   └── crawl/                   # Planner, seed fetch, bounded link discovery
├── data/                        # Fetched HTML (gitignored; created on persist)
├── tests/                       # Schema + registry + crawl tests
├── scripts/
│   ├── run_migrate.py
│   ├── import_seed_sources.py
│   ├── plan_crawl.py
│   ├── fetch_seeds.py
│   └── discover_links.py
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
| `document_jurisdictions` | Explicit applicability (never inferred from hierarchy alone) |

## Source registry notes

- Seed metadata is separate from downloaded documents
- Allowlist patterns use a narrow path scope (`https://host/path*`); matching requires exact scheme/host and genuine path descendants — not naive string prefixes
- HTTP verification and fetching use a transparent LifePilot user-agent and do not impersonate a browser
- Successful HTTP checks are **not** content authenticity; `robots.txt`, terms, and rate limits still apply
- Link discovery is bounded: only in-allowlist URLs from stored HTML; PDFs are listed but not fetched yet

## Stack (planned)

| Layer | Choice |
|---|---|
| Frontend | Next.js, React, TypeScript, Tailwind |
| Backend | Python, FastAPI, Pydantic |
| Database | PostgreSQL, SQLAlchemy, Alembic, pgvector |
| AI | LangGraph/LangChain, OpenAI and/or Anthropic |
| Infra | Docker, GitHub Actions |

## Next steps

PDF fetch for in-scope discovered links, then text extraction and RAG—built incrementally.
