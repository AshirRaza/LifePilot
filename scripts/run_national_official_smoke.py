"""Bounded live smoke for national official connectors (NL/IE/FR/SG).

Does not write to the knowledge base. Safe to run with network access.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from lifepilot.external.models import ExternalQuery, ProviderId  # noqa: E402
from lifepilot.external.router import ExternalKnowledgeRouter  # noqa: E402

CASES = [
    {
        "id": "NL-HSM",
        "jurisdiction": "NL",
        "pathway_id": "nl_highly_skilled_migrant",
        "query": "highly skilled migrant salary criterion",
    },
    {
        "id": "IE-CSEP",
        "jurisdiction": "IE",
        "pathway_id": "ie_critical_skills_employment_permit",
        "query": "Critical Skills Employment Permit remuneration",
    },
    {
        "id": "FR-E15",
        "jurisdiction": "FR",
        "pathway_id": "fr_passeport_talent",
        "query": "Passeport Talent categories carte de séjour",
    },
    {
        "id": "SG-EP",
        "jurisdiction": "SG",
        "pathway_id": "sg_employment_pass",
        "query": "Employment Pass eligibility salary requirements",
    },
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "runs" / "national_official_smoke.json")
    args = parser.parse_args(argv)

    rows = []
    with ExternalKnowledgeRouter(enable_disk_cache=False) as router:
        for case in CASES:
            result = router.query(
                ExternalQuery(
                    query=case["query"],
                    jurisdiction=case["jurisdiction"],
                    pathway_id=case["pathway_id"],
                    providers=(ProviderId.NATIONAL_OFFICIAL,),
                    max_results_per_provider=2,
                )
            )
            outcome = next(
                (o for o in result.outcomes if o.provider == ProviderId.NATIONAL_OFFICIAL),
                None,
            )
            items = outcome.items if outcome else []
            rows.append(
                {
                    **case,
                    "status": outcome.status.value if outcome else "MISSING",
                    "item_count": len(items),
                    "error_detail": outcome.error_detail if outcome else None,
                    "urls": [i.official_url for i in items],
                    "passage_preview": [
                        (i.passages[0][:180] if i.passages else "") for i in items[:1]
                    ],
                    "verification_state": [
                        i.verification_state.value for i in items
                    ],
                }
            )

    payload = {"cases": rows, "ok_count": sum(1 for r in rows if r["item_count"] > 0)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"ok_count={payload['ok_count']}/{len(rows)} wrote {args.out}")
        for r in rows:
            print(f"  {r['id']}: {r['status']} items={r['item_count']} {r.get('urls')}")
    return 0 if payload["ok_count"] == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
