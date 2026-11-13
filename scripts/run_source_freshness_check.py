"""Idempotent official-source freshness check using existing document versions.

Does not create a second versioning system. Uses DocumentVersion.retrieved_at /
content_sha256 and optional structured comparisons.

Examples:
  python scripts/run_source_freshness_check.py
  python scripts/run_source_freshness_check.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from lifepilot.db.models import Document, DocumentVersion  # noqa: E402
from lifepilot.db.session import get_session_factory  # noqa: E402

DEFAULT_POLICY = ROOT / "config" / "freshness" / "source_freshness_policy.json"
DEFAULT_OUT = ROOT / "data" / "runs" / "source_freshness_report.json"


def _load_policy(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _hours_since(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600.0


def check_freshness(session: Session, policy: dict[str, Any]) -> dict[str, Any]:
    categories = policy.get("categories") or {}
    rows_out: list[dict[str, Any]] = []
    for item in policy.get("priority_sources") or []:
        needle = (item.get("url_contains") or "").lower()
        cat = item.get("category") or "general_explanatory_guidance"
        limit_h = float((categories.get(cat) or {}).get("recheck_hours") or 336)
        docs = list(
            session.scalars(
                select(Document).where(Document.canonical_url.is_not(None)).limit(5000)
            )
        )
        matches = [d for d in docs if needle and needle in (d.canonical_url or "").lower()]
        if not matches:
            rows_out.append(
                {
                    **item,
                    "status": "missing_in_corpus",
                    "recheck_hours": limit_h,
                    "matched_documents": 0,
                }
            )
            continue
        for doc in matches[:5]:
            ver = session.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == doc.id,
                    DocumentVersion.is_current.is_(True),
                )
            )
            anchor = None
            if ver is not None:
                anchor = getattr(ver, "last_checked_at", None) or ver.retrieved_at
            age_h = _hours_since(anchor)
            stale = age_h is None or age_h > limit_h
            rows_out.append(
                {
                    **item,
                    "document_id": doc.id,
                    "canonical_url": doc.canonical_url,
                    "version_id": ver.id if ver else None,
                    "version_number": ver.version_number if ver else None,
                    "content_sha256": ver.content_sha256 if ver else None,
                    "retrieved_at": ver.retrieved_at.isoformat() if ver and ver.retrieved_at else None,
                    "last_checked_at": (
                        ver.last_checked_at.isoformat()
                        if ver and getattr(ver, "last_checked_at", None)
                        else None
                    ),
                    "age_hours": None if age_h is None else round(age_h, 2),
                    "recheck_hours": limit_h,
                    "status": "stale" if stale else "fresh_enough",
                    "note": (
                        "Stale means recheck is due; not confirmation that law changed. "
                        "Use scripts/run_knowledge_maintenance.py to recheck, classify, "
                        "and queue material changes."
                    ),
                }
            )
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy_id": policy.get("policy_id"),
        "counts": {
            "rows": len(rows_out),
            "fresh_enough": sum(1 for r in rows_out if r.get("status") == "fresh_enough"),
            "stale": sum(1 for r in rows_out if r.get("status") == "stale"),
            "missing_in_corpus": sum(
                1 for r in rows_out if r.get("status") == "missing_in_corpus"
            ),
        },
        "rows": rows_out,
        "limitations": [
            "No scheduler is bundled; invoke via cron/CI.",
            "Failed checks must not be treated as unchanged.",
            "Effective dates are not inferred by this script.",
            "Unverified fetches must not silently overwrite approved evidence "
            "(existing ingestion approval rules still apply).",
        ],
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Official-source freshness check")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    policy = _load_policy(Path(args.policy))
    factory = get_session_factory()
    with factory() as session:
        report = check_freshness(session, policy)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        c = report["counts"]
        print(
            f"freshness: rows={c['rows']} fresh={c['fresh_enough']} "
            f"stale={c['stale']} missing={c['missing_in_corpus']}"
        )
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
