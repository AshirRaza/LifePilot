# LifePilot

Evidence-backed immigration admin assistant (UK relocation MVP).

Official sources are ingested into a curated knowledge base. The agent retrieves with hybrid RAG (`external_mode=auto`), generates grounded Claude answers when configured, then runs unified legal verification. This is **not legal advice**. External hits stay `unverified_external` and are never auto-promoted into the KB.

**Stack:** Python 3.11+ · PostgreSQL + pgvector · LangGraph · local E5 embeddings · pytest

```text
retrieve → generate → ground → verify → respond
```

## Setup

```powershell
cp .env.example .env   # set POSTGRES_* / DATABASE_URL (prefer 127.0.0.1)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
docker compose up -d
$env:PYTHONPATH = "backend"
python scripts/check_database.py
alembic upgrade head
python scripts/import_seed_sources.py
```

## Commands

```powershell
$env:PYTHONPATH = "backend"

python scripts/ask_lifepilot.py --question "UK Skilled Worker requirements?" --jurisdiction GB
python scripts/run_agent_evidence.py --question "…" --jurisdiction GB
python scripts/run_knowledge_maintenance.py --dry-run --limit 10
pytest -q
```

More CLIs: [`scripts/README.md`](scripts/README.md). Config notes: [`.env.example`](.env.example). Maintenance: [`docs/maintenance/`](docs/maintenance/).

## Safety

- Only allowlisted HTTPS `approved` sources are fetchable.
- Automated checks ≠ human legal verification.
- Always confirm current rules on official government sites.
