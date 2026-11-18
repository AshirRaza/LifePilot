# LifePilot documentation

| Topic | Document |
| --- | --- |
| Project overview & quick start | [`../README.md`](../README.md) |
| Architecture | [`architecture.md`](architecture.md) |
| Temporal legal data model | [`temporal_data_model.md`](temporal_data_model.md) |
| Local development & configuration | [`../.env.example`](../.env.example), [`../scripts/README.md`](../scripts/README.md) |
| Private deployment & operations | [`ops/production.md`](ops/production.md) |
| Backup & restore | [`ops/backup_restore.md`](ops/backup_restore.md) |
| Artifact retention | [`maintenance/development_history.md`](maintenance/development_history.md) |
| Repository restructure | [`maintenance/production_repository_restructure.md`](maintenance/production_repository_restructure.md) |

## Release posture

| Gate | Status |
| --- | --- |
| Frontend integration | Approved |
| Private staging | Approved with limitations |
| Public production | **Not approved** |

Outstanding public-launch blockers: stronger public abuse controls, `transformers` vulnerability remediation with embedding regression, off-host disaster-recovery proof, installed maintenance scheduling/alerting, and human clearance of high-risk legal review queues.

Automated verification is **not** independent legal certification.
