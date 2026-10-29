"""Import configuration-driven government registry candidates.

Imports jurisdictions, authorities, and sources from JSON. New sources are
always created as ``candidate``. Never auto-approves. Optionally verifies URLs
and writes an approval manifest for human review.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.db.session import get_session_factory  # noqa: E402
from lifepilot.registry.config_loader import (  # noqa: E402
    RegistryConfigError,
    load_registry_config,
)
from lifepilot.registry.importer import import_registry_config  # noqa: E402
from lifepilot.registry.onboarding import (  # noqa: E402
    build_approval_manifest,
    write_approval_manifest,
)
from lifepilot.registry.urls import (  # noqa: E402
    InvalidRegistryUrlError,
    path_prefix_pattern,
    validate_seed_and_patterns,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Import LifePilot registry candidates from JSON. "
            "Does not crawl documents and does not auto-approve sources."
        )
    )
    parser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to registry JSON config.",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip live HTTP verification (still imports as candidates).",
    )
    parser.add_argument(
        "--manifest",
        type=str,
        default=None,
        help="Write approval manifest JSON to this path (default under data/runs/).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print import summary as JSON.",
    )
    args = parser.parse_args()

    try:
        config = load_registry_config(args.config)
    except RegistryConfigError as exc:
        raise SystemExit(f"config error: {exc}") from exc

    factory = get_session_factory()
    with factory() as session:
        report = import_registry_config(
            session,
            jurisdictions=config.jurisdictions,
            authorities=config.authorities,
            sources=config.sources,
            verify=not args.no_verify,
        )

        # Never approve during config import.
        approved_leaks = [s for s in report.sources if s.review_status == "approved"]
        if any(s.action == "created" and s.review_status != "candidate" for s in report.sources):
            session.rollback()
            raise SystemExit("refusing import: newly created source was not candidate")

        auth_name = {a.key: a.name for a in config.authorities}
        manifest_inputs = []
        for seed in config.sources:
            try:
                _normalized, patterns = validate_seed_and_patterns(
                    seed.seed_url,
                    [path_prefix_pattern(seed.seed_url), *seed.extra_allowed_patterns],
                    require_https=True,
                )
            except InvalidRegistryUrlError:
                patterns = [seed.seed_url]
            manifest_inputs.append(
                (seed, auth_name.get(seed.authority_key, seed.authority_key), patterns)
            )

        # Prefer verification results already collected during import when present.
        manifest = build_approval_manifest(
            sources=manifest_inputs,
            config_path=str(Path(args.config).resolve()),
            verify=not args.no_verify,
        )
        session.commit()

    manifest_path = Path(
        args.manifest
        if args.manifest
        else ROOT / "data" / "runs" / "approval_manifest_6c2.json"
    )
    write_approval_manifest(manifest, manifest_path)

    summary = {
        "jurisdictions_created": report.jurisdictions_created,
        "jurisdictions_updated": report.jurisdictions_updated,
        "authorities_created": report.authorities_created,
        "authorities_updated": report.authorities_updated,
        "sources_created": report.sources_created,
        "sources_updated": report.sources_updated,
        "sources_unchanged": report.sources_unchanged,
        "candidate_count": report.candidate_count,
        "approved_count": report.approved_count,
        "manifest_path": str(manifest_path),
        "approved_leaks_in_report": len(approved_leaks),
        "sources": [
            {
                "key": s.key,
                "action": s.action,
                "review_status": s.review_status,
                "error": s.error,
                "warning": s.warning,
                "verification": s.verification.summary if s.verification else None,
            }
            for s in report.sources
        ],
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("Registry config import complete (candidates only).")
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
            f"unchanged={report.sources_unchanged} "
            f"candidate={report.candidate_count} approved={report.approved_count}"
        )
        for item in report.sources:
            verify_bit = (
                f" | {item.verification.summary}" if item.verification is not None else ""
            )
            err = f" | error={item.error}" if item.error else ""
            warn = f" | warning={item.warning}" if item.warning else ""
            print(
                f"  - {item.key}: {item.action} status={item.review_status}"
                f"{verify_bit}{err}{warn}"
            )
        print(f"approval_manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
