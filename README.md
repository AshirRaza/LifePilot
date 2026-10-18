# LifePilot

AI-powered personal life administration platform. It acts as an intelligent case manager for complex life transitions—starting with international relocation, immigration admin, and employment onboarding in the UK.

## Current status

Early scaffolding only. So far the repo includes:

- Docker Compose Postgres with **pgvector** (`pg17`)
- Local env config (`.env` / `.env.example`)
- A small DB connectivity check script (`check_db.py`)

No application API or frontend yet.

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

If the password contains special characters (e.g. `!`, `@`, `#`), URL-encode them in `DATABASE_URL` (e.g. `!` → `%21`).

### 3. Create a virtual environment and install deps

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install python-dotenv SQLAlchemy "psycopg[binary]"
```

**macOS / Linux:**

```bash
python -m venv .venv
source .venv/bin/activate
pip install python-dotenv SQLAlchemy "psycopg[binary]"
```

### 4. Start PostgreSQL

From the repo root (Compose loads variables from `.env`):

```bash
docker compose up -d
```

Check that the container is healthy:

```bash
docker compose ps
```

### 5. Verify the database connection

With the venv activated:

```bash
python check_db.py
```

Expected output:

```text
PostgreSQL connection successful: 1
```

## Useful commands

| Command | Purpose |
|---|---|
| `docker compose up -d` | Start Postgres in the background |
| `docker compose down` | Stop containers (keeps data volume) |
| `docker compose down -v` | Stop and **delete** the Postgres data volume |
| `docker compose logs -f postgres` | Tail Postgres logs |
| `python check_db.py` | Smoke-test SQLAlchemy → Postgres |

## Project layout (so far)

```text
LifePilot/
├── compose.yaml      # Postgres + pgvector service
├── .env.example      # Shared env template (safe to commit)
├── .env              # Local secrets (gitignored)
├── check_db.py       # DB connectivity smoke test
├── .gitignore
└── README.md
```

## Stack (planned)

| Layer | Choice |
|---|---|
| Frontend | Next.js, React, TypeScript, Tailwind |
| Backend | Python, FastAPI, Pydantic |
| Database | PostgreSQL, SQLAlchemy, Alembic, pgvector |
| AI | LangGraph/LangChain, OpenAI and/or Anthropic |
| Infra | Docker, GitHub Actions |

## Next steps

Foundational backend architecture, then incremental feature work toward the UK graduate → employment MVP.
