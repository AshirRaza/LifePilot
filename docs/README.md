# LifePilot documentation

Canonical docs by topic. Prefer these paths over duplicating setup instructions.

| Topic | Document |
| --- | --- |
| Project overview & quick start | [`../README.md`](../README.md) |
| Architecture | [`architecture.md`](architecture.md) |
| Local development & configuration | [`../.env.example`](../.env.example), [`../scripts/README.md`](../scripts/README.md) |
| Private deployment & operations | [`ops/production.md`](ops/production.md) |
| Backup & restore | [`ops/backup_restore.md`](ops/backup_restore.md) |
| Knowledge-base maintenance | [`ops/production.md`](ops/production.md#maintenance-schedule), `scripts/run_knowledge_maintenance.py` |
| Evaluation & testing | [`../scripts/README.md`](../scripts/README.md#evaluation--regression) |
| Artifact retention | [`maintenance/development_history.md`](maintenance/development_history.md) |
| Phase reports (historical) | `data/runs/phase_9*.md` (local; gitignored under `data/`) |
| Backend cleanup (post-9D.6) | [`maintenance/final_backend_cleanup_report.md`](maintenance/final_backend_cleanup_report.md) |

## Release posture (Phase 9D.6)

| Gate | Status |
| --- | --- |
| Frontend integration | Approved |
| Private staging | Approved with limitations |
| Public production | **Not approved** |

Outstanding public-launch blockers include stronger public abuse controls, `transformers` vulnerability remediation with embedding regression, off-host disaster-recovery proof, installed maintenance scheduling/alerting, and human clearance of high-risk legal review queues.

Automated verification is **not** independent legal certification.
