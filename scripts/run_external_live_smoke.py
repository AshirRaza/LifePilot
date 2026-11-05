"""Optional live connectivity smoke for Phase 7 providers (read-only).

Does not write to the database or source registry.
Usage:
  python scripts/run_external_live_smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.external.models import ExternalQuery, ProviderId  # noqa: E402
from lifepilot.external.router import ExternalKnowledgeRouter  # noqa: E402

CHECKS = [
    ("govuk_content", ExternalQuery(pathway_id="gb_skilled_worker", providers=(ProviderId.GOVUK_CONTENT,), max_results_per_provider=1)),
    ("canada_open", ExternalQuery(query="immigration", jurisdiction="CA", providers=(ProviderId.CANADA_OPEN,), max_results_per_provider=2)),
    ("eu_opendata", ExternalQuery(query="migration", jurisdiction="EU", providers=(ProviderId.EU_OPENDATA,), max_results_per_provider=2)),
    ("eurlex_cellar", ExternalQuery(query="blue card", jurisdiction="EU", providers=(ProviderId.EURLEX_CELLAR,), max_results_per_provider=1)),
    ("us_federal_register", ExternalQuery(query="immigration", jurisdiction="US", providers=(ProviderId.US_FEDERAL_REGISTER,), max_results_per_provider=2)),
    ("eu_immigration_portal", ExternalQuery(jurisdiction="EU", providers=(ProviderId.EU_IMMIGRATION_PORTAL,), max_results_per_provider=1)),
]


def main() -> int:
    summary = []
    with ExternalKnowledgeRouter(enable_disk_cache=False) as router:
        for name, query in CHECKS:
            result = router.query(query)
            outcome = result.outcomes[0]
            row = {
                "provider": name,
                "status": outcome.status.value,
                "items": len(outcome.items),
                "error_detail": outcome.error_detail,
                "sample_title": outcome.items[0].title if outcome.items else None,
                "sample_url": outcome.items[0].official_url if outcome.items else None,
            }
            summary.append(row)
            print(f"{name}: {row['status']} items={row['items']} title={row['sample_title']}")
            if row["error_detail"]:
                print(f"  detail: {row['error_detail'][:200]}")
    print(json.dumps(summary, indent=2))
    # Exit 0 even if portal is OWNER_ACTION_REQUIRED — that is expected.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
