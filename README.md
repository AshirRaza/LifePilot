# LifePilot

Evidence-backed immigration information backend (UK relocation MVP first).

Official government sources are ingested into a curated knowledge base. The agent retrieves with hybrid RAG (`external_mode=auto`), drafts grounded Claude answers when configured, then runs unified legal verification. This is **not legal advice**. External hits remain `unverified_external` and are never auto-promoted into the KB.

**Stack:** Python 3.12 · PostgreSQL + pgvector · FastAPI · LangGraph · local E5 embeddings · pytest

```text
validate → evidence → generate → verify → respond
```

## Release status

| Gate | Status |
| --- | --- |
| Frontend integration | **Ready** |
| Private staging deployment | **Ready with limitations** |
| Public production launch | **Not approved** |

See [`docs/README.md`](docs/README.md) for blockers.

## Setup

```powershell
cp .env.example .env   # set POSTGRES_* / DATABASE_URL (prefer 127.0.0.1)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
docker compose up -d postgres
$env:PYTHONPATH = "backend"
python scripts/database/check_database.py
alembic upgrade head
python scripts/database/import_seed_sources.py
```

Configuration: [`.env.example`](.env.example).

## Core commands

```powershell
$env:PYTHONPATH = "backend"

python scripts/operations/ask_lifepilot.py --question "UK Skilled Worker requirements?" --jurisdiction GB
python scripts/operations/run_agent_evidence.py --question "…" --jurisdiction GB
python scripts/maintenance/run_knowledge_maintenance.py --dry-run --limit 10
python scripts/operations/run_api.py --host 127.0.0.1 --port 8000
pytest -q
```

## Private API

Endpoints: `POST /v1/answer`, `GET /health`, `GET /ready`, `GET /v1/ops/status` (auth).

```powershell
$env:LIFEPILOT_ENV = "production"
$env:LIFEPILOT_API_REQUIRE_AUTH = "1"
$env:LIFEPILOT_API_KEYS = "replace_with_long_random_api_key"
python scripts/operations/run_api.py --host 127.0.0.1 --port 8000

# Or Compose API profile
docker compose --profile api up -d --build
```

`/v1/answer` always uses `external_mode=auto` and unified verification.

## Package map

| Package | Role |
| --- | --- |
| `lifepilot.api` | FastAPI boundary |
| `lifepilot.agent` | LangGraph agent, grounding, verification |
| `lifepilot.rag` | Chunking, embeddings, internal retrieval |
| `lifepilot.external` | Official-source connectors |
| `lifepilot.maintenance` | Freshness recheck / change queue |
| `lifepilot.operations` | Locks, redaction, readiness |
| `lifepilot.evaluation` | Shared eval helpers (not production answers) |
| `lifepilot.db` | SQLAlchemy models/sessions |

## Documentation

| Topic | Link |
| --- | --- |
| Docs index | [`docs/README.md`](docs/README.md) |
| Architecture | [`docs/architecture.md`](docs/architecture.md) |
| Scripts | [`scripts/README.md`](scripts/README.md) |
| Production ops | [`docs/ops/production.md`](docs/ops/production.md) |
| Backup & restore | [`docs/ops/backup_restore.md`](docs/ops/backup_restore.md) |

## Safety

- Only allowlisted HTTPS `approved` sources are fetchable.
- Automated checks ≠ human legal verification.
- Confirm current rules on official government sites.
- Do not commit API keys, `.env` secrets, or database dumps.
- Known dependency debt: `transformers` CVEs — tracked, not resolved by restructuring.
