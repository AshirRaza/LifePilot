# LifePilot — Artifact retention and configuration notes

`data/` is gitignored. Keep operational and eval artifacts locally; do not
accumulate redundant narrative reports beyond the phase deliverables listed below.

## Keep (immutable / operational)

| Path | Why |
| --- | --- |
| `config/eval/agent_qa_benchmark_v1.json` | Fixed 50-question benchmark |
| `config/eval/agent_qa_supplemental_9d1.json` | Supplemental + held-out X16–X20 |
| `config/eval/reference_evidence_packets_9d1.json` | Independent reference URL scaffolding |
| `config/eval/high_risk_legal_review_queue.json` | High-risk legal review state |
| `config/eval/verification_reliability_9d3.json` | 9D.3 reliability fixture index |
| `config/eval/performance_cases_9d4.json` | 9D.4 performance case set |
| `config/freshness/source_freshness_policy.json` | Recheck intervals |
| `config/external/national_official_sources.json` | NL/IE/FR/SG official URL catalog |
| `data/runs/phase_9d1_comparative_answer_evaluation.md` | 9D.1 report |
| `data/runs/phase_9d2_unified_legal_verification_report.md` | 9D.2 report |
| `data/runs/phase_9d3_verification_reliability_report.md` | 9D.3 report |
| `data/runs/phase_9d4_performance_optimization_report.md` | 9D.4 report |
| `data/runs/phase_9d5_production_reliability_report.md` | 9D.5 report |
| `data/runs/phase_9d6_final_backend_acceptance_report.md` | 9D.6 acceptance |
| `data/runs/comparative_answer_eval/` | 9D.1 paired results / worksheet |
| `data/runs/unified_verification_eval/` | 9D.2 results |
| `data/runs/verification_reliability_eval/` | 9D.3 offline results |
| `data/runs/performance_optimization_9d4/` | 9D.4 results |
| `data/runs/production_reliability_9d5/` | 9D.5 ops evidence |
| `data/runs/final_backend_acceptance_9d6/` | 9D.6 evidence (incl. held-out baseline) |
| `data/runs/final_backend_acceptance_9d6/held_out_x16_x20/first_run_baseline.jsonl` | Immutable blind X16–X20 first run |
| `data/runs/grounded_qa_eval/baseline_9c1/` | Immutable 9C.1 machine baseline |
| `data/runs/coverage_matrix.csv` / `evidence_matrix.csv` | Coverage gold / inventory |
| `data/runs/human_review_packets/` / `review_packets/` | Human review artifacts |
| `data/runs/backup_manifest.json` | Backup inventory |
| `docs/maintenance/final_backend_cleanup_report.md` | Post-9D.6 cleanup record |

## Regenerable (safe to delete; scripts recreate)

- `run_source_freshness_check.py` → freshness JSON reports
- `run_knowledge_maintenance.py` → `knowledge_maintenance/` / `latest_report.json`
- `run_national_official_smoke.py` → smoke JSON
- Eval runners → `results.jsonl` / summary JSON (except immutable baselines above)
- Disposable `_*.py` / `_*.txt` scratch helpers under `data/runs/`

Do **not** overwrite held-out first-run baselines or rewrite historical evaluation JSONL.

## Agent configuration (active)

| Setting | Default | Controls |
| --- | --- | --- |
| `AgentRequest.retrieval.external_mode` | `auto` | Hybrid retrieval policy |
| `LIFEPILOT_VERIFICATION_ENABLED` | true | Unified legal verification node |
| `LIFEPILOT_VERIFICATION_MAX_EXTERNAL` | 0 | Extra official corroboration fetches |
| `LIFEPILOT_AGENT_EXTERNAL_FALLBACK` | false | Deprecated no-op (compat only) |

## Known unresolved debt (do not mark resolved here)

- `transformers` vulnerability findings from Phase 9D.6 `pip-audit` (requires dedicated upgrade + embedding regression).
- Public production blockers listed in `docs/README.md` / 9D.6 acceptance report.

See `.env.example` and `scripts/README.md`.
