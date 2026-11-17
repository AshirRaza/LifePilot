# LifePilot — Production Operations (Phase 9D.5)

Private/internal backend. **Not legal advice.** Completing these controls does not certify immigration legal accuracy.

## Deployment boundary

| Component | Role |
| --- | --- |
| `api` (FastAPI/uvicorn) | `/health`, `/ready`, `/v1/answer`, `/v1/ops/status` |
| PostgreSQL + pgvector | Curated KB + embeddings |
| `scripts/maintenance/run_knowledge_maintenance.py` | Scheduled source rechecks |
| Claude (Anthropic) | Grounded generation only |

No Kubernetes/Redis/Celery required for the initial deployment.

## Quick start (host)

```powershell
cp .env.example .env
# Set DATABASE_URL, ANTHROPIC_API_KEY, LIFEPILOT_ENV=production,
# LIFEPILOT_API_REQUIRE_AUTH=1, LIFEPILOT_API_KEYS=...
pip install -r requirements.txt
docker compose up -d postgres
$env:PYTHONPATH = "backend"
alembic upgrade head
python scripts/operations/run_api.py --host 127.0.0.1 --port 8000
```

## Compose with API profile

```powershell
docker compose --profile api up -d --build
```

Postgres is bound to `127.0.0.1` only. API likewise. Do not publish Postgres publicly.

**Migrations:** run `alembic upgrade head` deliberately (host or one-off container). Do not put migrations in an uncontrolled restart loop.

## Authentication

- Production: `LIFEPILOT_API_REQUIRE_AUTH=1` and `LIFEPILOT_API_KEYS=key1,key2`
- Send `Authorization: Bearer <key>` or `X-API-Key: <key>`
- `/health` is unauthenticated (liveness)
- `/ready` returns a public boolean readiness summary
- `/v1/ops/status` requires auth (detailed maintenance/freshness)

Fail closed: production without keys refuses to serve authenticated routes meaningfully (503 when auth required but unconfigured).

## Answer API

`POST /v1/answer` always uses:

- `external_mode=auto`
- Unified legal verification enabled
- Existing `run_agent` LangGraph path

Clients cannot disable verification or force unsafe retrieval modes through this API.

Mutating requests must send `Content-Length`. Oversized bodies (`LIFEPILOT_API_MAX_REQUEST_BYTES`, default 32 KiB) are rejected with HTTP 413. Missing length on POST/PUT/PATCH returns HTTP 411.

## Maintenance schedule

Examples (not auto-installed):

```text
# cron — daily 03:15
15 3 * * * cd /path/LifePilot && .venv/bin/python scripts/maintenance/run_knowledge_maintenance.py --execute --limit 25

# compose one-shot
docker compose --profile maintenance run --rm maintenance
```

Concurrent runs are blocked by `data/locks/knowledge_maintenance.lock`.

Exit code `1` when execute mode records failed fetches. Alerts: `data/runs/knowledge_maintenance/freshness_alerts.json`.

Material/uncertain legal changes remain queued for human review — never auto-approved.

## Observability

Structured logs include request id, status, retrieval decision, token counts, estimated cost, verification timing. Questions are not logged by default (`LIFEPILOT_API_LOG_QUESTIONS=0`).

## Backups

See [`backup_restore.md`](backup_restore.md).
