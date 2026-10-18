"""Import curated official government seed URLs into the source registry."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.db.session import get_session_factory  # noqa: E402
from lifepilot.registry.importer import import_seed_sources  # noqa: E402
from lifepilot.registry.review import ReviewError, set_source_review_status  # noqa: E402
from lifepilot.registry.seeds import SOURCES  # noqa: E402
from lifepilot.registry.urls import normalize_url  # noqa: E402


def _resolve_normalized_url(seed_key: str) -> str:
    for seed in SOURCES:
        if seed.key == seed_key:
            return normalize_url(seed.seed_url)
    raise SystemExit(f"Unknown seed key: {seed_key}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Upsert LifePilot official seed sources. "
            "Does not crawl or download documents. "
            "Does not auto-approve sources."
        )
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip live HTTP reachability checks (reported only; never changes approval).",
    )
    parser.add_argument(
        "--set-status",
        action="append",
        metavar="SEED_KEY=STATUS",
        default=[],
        help=(
            "Explicit review operation, e.g. "
            "--set-status gb_skilled_worker_visa=approved. "
            "Allowed statuses: approved, candidate, rejected."
        ),
    )
    args = parser.parse_args()

    factory = get_session_factory()
    with factory() as session:
        report = import_seed_sources(session, verify=not args.no_verify)

        for item in args.set_status:
            if "=" not in item:
                raise SystemExit(
                    f"Invalid --set-status value {item!r}; expected KEY=STATUS"
                )
            key, status = item.split("=", 1)
            try:
                set_source_review_status(
                    session,
                    seed_url_normalized=_resolve_normalized_url(key.strip()),
                    status=status.strip(),
                )
                print(f"Explicit review: {key.strip()} -> {status.strip()}")
            except ReviewError as exc:
                raise SystemExit(f"Review failed for {key}: {exc}") from exc

        session.commit()

    print("Source registry import complete.")
    print(
        f"jurisdictions: created={report.jurisdictions_created} "
        f"updated={report.jurisdictions_updated}"
    )
    print(
        f"authorities: created={report.authorities_created} "
        f"updated={report.authorities_updated}"
    )
    print(
        f"sources: created={report.sources_created} "
        f"updated={report.sources_updated} "
        f"unchanged={report.sources_unchanged}"
    )
    print(
        f"review_status: approved={report.approved_count} "
        f"candidate={report.candidate_count} "
        f"rejected={report.rejected_count}"
    )
    for item in report.sources:
        verify_bit = ""
        if item.verification is not None:
            verify_bit = f" | {item.verification.summary}"
        err_bit = f" | error={item.error}" if item.error else ""
        print(
            f"  - {item.key}: {item.action} "
            f"status={item.review_status} "
            f"normalized={item.seed_url_normalized}{verify_bit}{err_bit}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
