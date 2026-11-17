# LifePilot — Artifact retention

`data/` and `archive/` are gitignored local trees. Keep operational and compact
eval fixtures; do not accumulate obsolete phase narrative reports in the active
working tree.

## Keep (authoritative / active)

| Path | Why |
| --- | --- |
| `config/eval/agent_qa_benchmark_v1.json` | Fixed 50-question benchmark |
| `config/eval/agent_qa_supplemental_9d1.json` | Supplemental + held-out X16–X20 definitions |
| `config/eval/held_out_x16_x20_first_run_baseline.jsonl` | Immutable blind acceptance answers |
| `config/eval/reference_evidence_packets_9d1.json` | Reference URL scaffolding |
| `config/eval/high_risk_legal_review_queue.json` | High-risk legal review state |
| `config/eval/verification_reliability.json` | Verification reliability fixtures |
| `config/eval/performance_cases.json` | Performance case set |
| `config/freshness/source_freshness_policy.json` | Recheck intervals |
| `config/external/national_official_sources.json` | Official URL catalog |
| `data/runs/review_packets/` | Material-change review artifacts |
| `data/runs/human_review_packets/` | Pathway human-review packets |
| `data/runs/knowledge_maintenance/` | Operational maintenance reports |
| `data/runs/coverage_matrix.csv` / `evidence_matrix.csv` | Coverage gold / inventory |
| `data/runs/backup_manifest.json` | Backup inventory |
| `data/runs/blocked_evidence_recoveries.csv` | Blocked-evidence tracking |

## Archived locally (not active source)

Historical phase markdown reports and bulky eval result directories live under
`archive/historical_phase_reports/` on developer machines after restructuring.
They are not required for runtime or CI.

## Regenerable

- Maintenance / freshness / smoke JSON reports
- Eval `results.jsonl` outputs (except held-out first-run baseline above)

## Agent configuration (active)

| Setting | Default | Controls |
| --- | --- | --- |
| `AgentRequest.retrieval.external_mode` | `auto` | Hybrid retrieval policy |
| `LIFEPILOT_VERIFICATION_ENABLED` | true | Unified legal verification |
| `LIFEPILOT_VERIFICATION_MAX_EXTERNAL` | 0 | Extra corroboration fetches |
| `LIFEPILOT_AGENT_EXTERNAL_FALLBACK` | false | Deprecated no-op (compat) |

## Unresolved debt

- `transformers` vulnerability findings (requires upgrade + embedding regression)
- Public-launch blockers listed in `docs/README.md`
