# Backup and restore

LifePilot stores authoritative corpus state in PostgreSQL. Host `data/` holds raw fetches and operational artifacts. Secrets in `.env` are **not** part of the DB backup — store them separately.

## Backup (repeatable)

Prefer writing dumps to `./backups` (compose mounts this on Postgres as `/backups`).

```powershell
# Ensure backups dir exists on the host (outside the disposable DB volume)
mkdir backups -Force

# Logical dump via the Postgres container
docker compose exec -T postgres pg_dump -U $env:POSTGRES_USER -d $env:POSTGRES_DB -Fc -f /backups/lifepilot_$(Get-Date -Format yyyyMMdd).dump
```

Corpus reliability helper (manifest + optional data archive):

```powershell
$env:PYTHONPATH = "backend"
python scripts/database/run_corpus_reliability.py --help
```

**Retention recommendation:** keep ≥7 daily dumps and ≥4 weekly dumps off the database volume (object storage or another disk). Encrypt backups at rest when they leave the host.

## Restore to an isolated database (required safety)

Never restore into `lifepilot` or `lifepilot_test` via the guarded helpers. Use a name ending `_restore_test`:

```powershell
docker compose exec postgres psql -U lifepilot -d postgres -c "CREATE DATABASE lifepilot_restore_test"
docker compose exec -T postgres pg_restore -U lifepilot -d lifepilot_restore_test --clean --if-exists /backups/lifepilot_YYYYMMDD.dump
```

Then point a temporary `DATABASE_URL` at the restore DB and run `python scripts/database/check_database.py` / count checks.

The Python restore guards in `lifepilot.coverage.corpus_reliability.backup` refuse protected database names.

## Recovery notes

| Failure | Action |
| --- | --- |
| Lost DB volume | Create fresh Postgres volume → restore dump → `alembic upgrade head` if needed → verify counts |
| Lost `data/` | Restore from archived tarball / object storage; re-run failed fetches if needed |
| Bad migration | Restore pre-migration dump to `_restore_test`, validate, then plan forward fix — do not blindly downgrade production |
| Corrupted deploy | Redeploy image from known tag; restore DB if schema/data drifted |

## What must survive restore

- Source registry and approval state
- Documents / versions / extractions
- Chunks + embeddings
- Pending material-change / review artifacts
- Config under `config/` (version-controlled; restore from git if needed)
