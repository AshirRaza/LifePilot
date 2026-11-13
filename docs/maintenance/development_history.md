# LifePilot — Development history index

Concise map of archived phase reports. Full text lives under
`data/runs/archive/phase_history/` (local; `data/` is gitignored).

| Artifact | Topic |
|---|---|
| `phase_7_report.md` | External knowledge connectors |
| `phase_8a_readiness_report.md` … `phase_8h_report.md` | Coverage, corpus reliability, hybrid RAG, remediation |
| `phase_8d1_cleanup_report.md` | Earlier cleanup pass |
| `phase_9a_report.md` | LangGraph agent contracts / evidence graph |
| `phase_9b_report.md` / `phase_9b1_latency_report.md` | Evidence orchestration + embedder reuse latency fix |
| `phase_9c_report.md` | Anthropic Claude grounded answers |
| `codebase_cleanup_report.md` | Nov 2026 pre–9C cleanup |

## Active evaluation / audit records (not archived)

| Path | Role |
|---|---|
| `config/eval/agent_qa_benchmark_v1.json` | Fixed 50-question benchmark |
| `data/runs/phase_9c1_evaluation_report.md` | Human-readable 9C.1 E2E evaluation |
| `data/runs/phase_9c2_grounding_audit_report.md` | Grounding audit + hardening |
| `data/runs/grounded_qa_eval/baseline_9c1/` | Immutable 9C.1 machine baseline |
| `data/runs/grounded_qa_eval/phase_9c2_*.jsonl` | Offline audit, re-ground, paid regression |
| `data/runs/human_review_packets/` | Pathway human-review packets |
| `data/runs/corpus_status_report.md` + matrices | Corpus / coverage operational snapshots |

Phase 9C.3 / 9D were not started as part of the post–9C.2 cleanup.
