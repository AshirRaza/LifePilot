# LifePilot

AI-powered personal life administration platform. It acts as an intelligent case manager for complex life transitions—starting with international relocation, immigration admin, and employment onboarding in the UK.

## Current status

Phases completed so far:

1. **Local Postgres** — Docker Compose with PostgreSQL 17 + pgvector; connection smoke test
2. **Knowledge schema** — SQLAlchemy 2.x models + Alembic migration for government knowledge collection (`jurisdictions`, `authorities`, `sources`, `documents`, `document_versions`, `document_jurisdictions`)

No crawler, API, or frontend yet.

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

### 7. Run tests

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
| `pytest -v` | Run schema/constraint tests |

### Inspect tables in Postgres

```bash
docker compose exec -T postgres psql -U lifepilot -d lifepilot
```

```sql
\dt
\d documents
\d document_versions
```

## Project layout (so far)

```text
LifePilot/
├── compose.yaml              # Postgres + pgvector
├── .env.example              # Env template (safe to commit)
├── .env                      # Local secrets (gitignored)
├── check_db.py               # DB connectivity smoke test
├── requirements.txt
├── alembic.ini
├── alembic/                  # Migrations
├── backend/lifepilot/db/     # SQLAlchemy models + session
├── tests/                    # Schema constraint tests
├── scripts/run_migrate.py    # Optional migrate helper
├── pytest.ini
├── .gitignore
└── README.md
```

## Knowledge schema (overview)

| Table | Purpose |
|---|---|
| `jurisdictions` | Countries, regions, municipalities, EU (supranational), etc. |
| `authorities` | Publishing government bodies |
| `sources` | Curated seed URLs + approval status |
| `documents` | One row per normalized URL / language variant |
| `document_versions` | Historical raw snapshots; at most one `is_current` per document |
| `document_jurisdictions` | Explicit applicability (never inferred from hierarchy alone) |

## Stack (planned)

| Layer | Choice |
|---|---|
| Frontend | Next.js, React, TypeScript, Tailwind |
| Backend | Python, FastAPI, Pydantic |
| Database | PostgreSQL, SQLAlchemy, Alembic, pgvector |
| AI | LangGraph/LangChain, OpenAI and/or Anthropic |
| Infra | Docker, GitHub Actions |

## Next steps

Crawler / document ingestion, then extracted text and RAG layers—built incrementally on this schema.
