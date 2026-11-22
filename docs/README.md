# LifePilot documentation

| Topic | Document |
| --- | --- |
| Project overview & quick start | [`../README.md`](../README.md) |
| Architecture | [`architecture.md`](architecture.md) |
| Temporal legal data model | [`temporal_data_model.md`](temporal_data_model.md) |
| Phase 10C.3 qualifier verification | [`phase_10c3_qualifier_verification_report.md`](phase_10c3_qualifier_verification_report.md) |
| Phase 10C.4 claim provenance & API | [`phase_10c4_claim_provenance_api_report.md`](phase_10c4_claim_provenance_api_report.md) |
| Phase 10C.5 evidence integrity acceptance | [`phase_10c5_evidence_integrity_acceptance_report.md`](phase_10c5_evidence_integrity_acceptance_report.md) |
| Phase 10D.1 legal review governance data model | [`phase_10d1_legal_review_governance_report.md`](phase_10d1_legal_review_governance_report.md) |
| Phase 10D.2 authorized review workflow | [`phase_10d2_legal_review_workflow_report.md`](phase_10d2_legal_review_workflow_report.md) |
| Phase 10D.3 source-change invalidation | [`phase_10d3_source_change_invalidation_report.md`](phase_10d3_source_change_invalidation_report.md) |
| Phase 10D.4 governance integrity enforcement | [`phase_10d4_governance_integrity_report.md`](phase_10d4_governance_integrity_report.md) |
| Phase 10D.5 integrated governance acceptance | [`phase_10d5_governance_acceptance_report.md`](phase_10d5_governance_acceptance_report.md) |
| Governance migration runbook (staging) | [`ops/governance_migration_runbook.md`](ops/governance_migration_runbook.md) |
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
