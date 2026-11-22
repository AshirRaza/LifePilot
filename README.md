# LifePilot

LifePilot is an AI-assisted personal administration platform for complex life transitions—starting with international relocation and UK immigration admin.

It is built as an **evidence-backed immigration intelligence backend**: answers are grounded in curated official sources, checked by deterministic verification, and kept separate from human legal approval. It is **not legal advice**.

## Why it exists

International movers face fragmented government guidance, documents, deadlines, and task dependencies. Generic chatbots invent requirements or treat model confidence as authority. LifePilot keeps persistent structure, cites official material, and fails closed when evidence is missing or outdated.

**MVP focus:** international graduates moving into UK employment (profile → roadmap → document-aware workflows → change-aware updates).

## What it does

- Ingests allowlisted official sources into PostgreSQL + pgvector
- Retrieves with hybrid RAG and optional external official connectors
- Drafts grounded answers (Claude when configured), then runs claim-level legal verification
- Distinguishes **mechanical support** from **human-verified** legal rules
- Tracks source changes and routes high-risk items through human review governance

```text
validate → retrieve evidence → generate → verify → respond
```

## Stack

Python 3.12 · FastAPI · LangGraph · PostgreSQL + pgvector · local E5 embeddings · Alembic · pytest

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

See [`.env.example`](.env.example) for configuration.

## Common commands

```powershell
$env:PYTHONPATH = "backend"

python scripts/operations/ask_lifepilot.py --question "UK Skilled Worker requirements?" --jurisdiction GB
python scripts/operations/run_api.py --host 127.0.0.1 --port 8000
python scripts/maintenance/run_knowledge_maintenance.py --dry-run --limit 10
pytest -q
```

Private API: `POST /v1/answer`, `GET /health`, `GET /ready`. Auth via `LIFEPILOT_API_KEYS` when required.

## Package map

| Package | Role |
| --- | --- |
| `lifepilot.api` | FastAPI boundary |
| `lifepilot.agent` | Orchestration, grounding, verification |
| `lifepilot.rag` | Chunking, embeddings, internal retrieval |
| `lifepilot.external` | Official-source connectors |
| `lifepilot.governance` | Human legal-review workflows & authority checks |
| `lifepilot.maintenance` | Freshness recheck / change queue |
| `lifepilot.db` | Models and sessions |

## Release status

| Gate | Status |
| --- | --- |
| Frontend integration | Ready |
| Private staging | Ready with limitations |
| Public production | **Not approved** |

Details and blockers: [`docs/README.md`](docs/README.md).

## Safety

- Only allowlisted HTTPS approved sources are fetchable.
- Automated verification is not human legal certification.
- Confirm current rules on official government sites.
- Do not commit `.env`, API keys, or database dumps.

## Docs

| Topic | Link |
| --- | --- |
| Docs index | [`docs/README.md`](docs/README.md) |
| Architecture | [`docs/architecture.md`](docs/architecture.md) |
| Scripts | [`scripts/README.md`](scripts/README.md) |
| Production ops | [`docs/ops/production.md`](docs/ops/production.md) |
