# LifePilot — What to keep under `data/` and `docs/`

`data/` is gitignored. Keep operational and eval artifacts only; do not
accumulate narrative phase reports.

## Keep

| Path | Why |
|---|---|
| `config/eval/agent_qa_benchmark_v1.json` | Fixed 50-question benchmark |
| `config/eval/high_risk_legal_review_queue.json` | High-risk legal review state |
| `config/freshness/source_freshness_policy.json` | Recheck intervals |
| `config/external/national_official_sources.json` | NL/IE/FR/SG official URL catalog |
| `data/runs/grounded_qa_eval/baseline_9c1/` | Immutable 9C.1 machine baseline |
| `data/runs/grounded_qa_eval/phase_9c2_*.jsonl` | Grounding audit / regression inputs |
| `data/runs/coverage_matrix.csv` | Retrieval eval / coverage gold |
| `data/runs/evidence_matrix.csv` | Evidence inventory |
| `data/runs/corpus_status_report.md` | Corpus readiness snapshot |
| `data/runs/blocked_evidence_recoveries.csv` | Blocked-evidence tracking |
| `data/runs/backup_manifest.json` | Backup inventory |
| `data/runs/human_review_packets/` | Pathway human-review packets |
| `data/runs/review_packets/` | Material-change / E15 review packets |

## Regenerable (delete anytime; scripts recreate)

- `run_source_freshness_check.py` → `source_freshness_report.json`
- `run_knowledge_maintenance.py` → `knowledge_maintenance_report.json`
- `run_national_official_smoke.py` → `national_official_smoke.json`
- `run_grounded_qa_eval.py` → eval markdown / `results.jsonl`
- `write_grounding_audit_report.py` → grounding audit markdown

Do not delete `baseline_9c1/` or rewrite historical evaluation JSONL artifacts.
