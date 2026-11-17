# LifePilot — Backend architecture

## Runtime path

```text
HTTP/CLI → validate → evidence retrieval → grounded generation → legal verification → finalize
```

Production answering always uses `external_mode=auto` and unified claim-level verification (`verify_material_claims`). The private API (`lifepilot.api`) is a thin boundary over `run_agent` and cannot disable those defaults.

## Package map (`backend/lifepilot/`)

| Package | Responsibility |
| --- | --- |
| `api/` | FastAPI app, auth, rate limits, schemas |
| `agent/` | LangGraph orchestration, generation, grounding, verification, pathway safeguards |
| `rag/` | Chunking, embeddings, internal retrieval |
| `external/` | Official-source connectors, HTTP client, allowlists, caching |
| `maintenance/` | Freshness recheck, change classification, pending review |
| `operations/` | Locks, secret redaction, readiness probes |
| `db/` | SQLAlchemy models and sessions |
| `ingest/`, `extract/`, `crawl/`, `registry/` | Corpus acquisition and registry |
| `coverage/` | Offline corpus/pathway analytics |
| `evaluation/` | Shared evaluation helpers (reuse production agent) |

## Scripts layout

```text
scripts/
  operations/     # API, ask, query
  maintenance/    # KB recheck, ingest, crawl helpers
  database/       # migrate, import, corpus reliability
  evaluation/     # grounded QA, verification, performance
  corpus/         # offline coverage analytics
```

## Naming conventions

| Kind | Convention | Example |
| --- | --- | --- |
| Modules / packages | `snake_case` | `retrieval_policy.py` |
| Classes | `PascalCase` | `EvidenceOrchestrator` |
| Functions / variables | `snake_case` | `verify_material_claims` |
| Constants | `UPPER_SNAKE_CASE` | `DEFAULT_EXTERNAL_TIMEOUT` |
| Env vars | `LIFEPILOT_*` | `LIFEPILOT_VERIFICATION_ENABLED` |
| Tests | `test_<behavior>.py` | `test_api_security.py` |

## Deterministic vs LLM

- **Deterministic:** dates, pathway attribution, claim verification rules, allowlists, maintenance promotion policy.
- **LLM:** NLU, grounded answer drafting and explanations.

LLMs never auto-approve material legal changes.

## Related docs

- Operations: [`ops/production.md`](ops/production.md)
- Scripts: [`../scripts/README.md`](../scripts/README.md)
