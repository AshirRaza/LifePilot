"""Unified bounded government document ingestion (dry-run by default).

Modes:
  full (default) — fetch/discover approved sources, then extract/compare.
  process-existing — extract/compare stored raw versions only (zero HTTP).

Does not widen allowlists, bypass bot protection, or run unbounded crawls.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.crawl.policy import fetcher_policy  # noqa: E402
from lifepilot.db.session import get_session_factory  # noqa: E402
from lifepilot.ingest.pipeline import (  # noqa: E402
    MODE_FULL,
    MODE_PROCESS_EXISTING,
    IngestLimits,
    run_ingestion,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bounded ingestion pipeline over approved sources. "
            "Defaults to dry-run (no network, no writes). "
            "Pass --execute to apply mutations within safety limits."
        )
    )
    parser.add_argument(
        "--mode",
        choices=[MODE_FULL, MODE_PROCESS_EXISTING],
        default=MODE_FULL,
        help=(
            "full: fetch/discover + extract/compare. "
            "process-existing: extract/compare backlog only (no HTTP)."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform bounded work (explicit opt-in; dry-run otherwise).",
    )
    parser.add_argument(
        "--source-id",
        type=int,
        action="append",
        default=None,
        help=(
            "Limit to one or more approved sources.id values (repeatable). "
            "Cannot combine with --all-approved or --country."
        ),
    )
    parser.add_argument(
        "--all-approved",
        action="store_true",
        help="Select every approved source (ordered by id). Cannot combine with --source-id.",
    )
    parser.add_argument(
        "--country",
        type=str,
        default=None,
        help=(
            "Limit to approved sources whose jurisdiction country_code or code "
            "matches this ISO/jurisdiction code (e.g. SG, CA, EU). "
            "Cannot combine with --source-id. Does not expand EU into member states."
        ),
    )
    parser.add_argument(
        "--max-sources",
        type=int,
        default=1,
        help="Max approved sources when neither --source-id nor --all-approved (default: 1).",
    )
    parser.add_argument(
        "--max-fetches",
        type=int,
        default=5,
        help="Global max document fetch attempts this run (full mode; default: 5).",
    )
    parser.add_argument(
        "--max-html-per-source",
        type=int,
        default=3,
        help="Max discovered HTML fetches per source (full mode; default: 3).",
    )
    parser.add_argument(
        "--max-binary-per-source",
        type=int,
        default=2,
        help="Max discovered PDF/DOCX fetches per source (full mode; default: 2).",
    )
    parser.add_argument(
        "--max-extractions",
        type=int,
        default=10,
        help="Max structured extractions this run (default: 10).",
    )
    parser.add_argument(
        "--max-comparisons",
        type=int,
        default=10,
        help="Max content comparisons this run (default: 10).",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="Max frontier BFS depth from seed (default: 3).",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Override storage root (default: ./data or LIFEPILOT_DATA_DIR).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full structured report as JSON.",
    )
    args = parser.parse_args()

    for name in (
        "max_sources",
        "max_fetches",
        "max_html_per_source",
        "max_binary_per_source",
        "max_extractions",
        "max_comparisons",
        "max_depth",
    ):
        if getattr(args, name) < 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be >= 0")
    if args.max_sources < 1:
        raise SystemExit("--max-sources must be >= 1")
    source_ids = list(args.source_id) if args.source_id else None
    if source_ids and args.all_approved:
        raise SystemExit("cannot combine --source-id with --all-approved")
    if source_ids and args.country:
        raise SystemExit("cannot combine --source-id with --country")

    limits = IngestLimits(
        max_sources=args.max_sources,
        max_fetches=args.max_fetches,
        max_html_per_source=args.max_html_per_source,
        max_binary_per_source=args.max_binary_per_source,
        max_extractions=args.max_extractions,
        max_comparisons=args.max_comparisons,
        max_depth=args.max_depth,
    )
    policy = fetcher_policy(
        max_discovered_html_fetches_per_run=limits.max_html_per_source,
        max_discovered_binary_fetches_per_run=limits.max_binary_per_source,
        max_depth=limits.max_depth,
    )

    factory = get_session_factory()
    with factory() as session:
        report = run_ingestion(
            session,
            execute=args.execute,
            mode=args.mode,
            source_ids=source_ids,
            all_approved=args.all_approved,
            country=args.country,
            limits=limits,
            policy=policy,
            storage_root=args.data_dir,
            write_report_file=args.execute,
        )
        if not args.execute:
            session.rollback()

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if not report.failures else 1

    run_mode = "execute" if args.execute else "dry-run"
    print(f"LifePilot ingestion ({run_mode}, mode={report.mode})")
    print(f"limits: {report.limits}")
    if report.selected_sources:
        print("selected sources:")
        for src in report.selected_sources:
            print(
                f"  - id={src['source_id']} {src.get('name')} "
                f"status={src.get('review_status')} seed={src.get('seed_url')}"
            )
    else:
        print("selected sources: (none)")
    if report.exclusions:
        print("exclusions:")
        for item in report.exclusions:
            print(f"  - {item}")
    if report.planned:
        print("planned:")
        for key, value in report.planned.items():
            print(f"  - {key}: {value}")
    if args.execute:
        print(
            f"fetches: attempted={report.fetches_attempted} "
            f"new={report.fetches_new_version} unchanged={report.fetches_unchanged} "
            f"skipped={report.fetches_skipped} failed={report.fetches_failed} "
            f"blocked={report.fetches_blocked}"
        )
        if report.attempted_urls:
            print("attempted URLs:")
            for item in report.attempted_urls:
                print(
                    f"  - [{item.get('status')}] http={item.get('http_status')} "
                    f"{item.get('url')}"
                )
                if item.get("message"):
                    print(f"      {item['message']}")

        print(
            f"extractions: success={report.extractions_success} "
            f"unchanged={report.extractions_unchanged} "
            f"skipped={report.extractions_skipped} empty={report.extractions_empty} "
            f"failed={report.extractions_failed}"
        )
        print(
            f"comparisons: unchanged={report.comparisons_unchanged} "
            f"changed={report.comparisons_changed} "
            f"unavailable={report.comparisons_unavailable}"
        )
        if report.extraction_backlog_remaining is not None:
            print(
                f"backlog remaining: extractions={report.extraction_backlog_remaining} "
                f"comparisons={report.comparison_backlog_remaining} "
                f"frontier_pending={report.frontier_pending_remaining}"
            )
        print(
            f"frontier: fetched={report.frontier_fetched} "
            f"skipped={report.frontier_skipped} blocked={report.frontier_blocked} "
            f"failed={report.frontier_failed} out_of_scope={report.frontier_out_of_scope} "
            f"children_enqueued={report.frontier_children_enqueued} "
            f"transient_retries={report.frontier_transient_retries}"
        )
        print(
            f"sitemaps: attempted={report.sitemap_fetches_attempted} "
            f"ok={report.sitemap_fetches_ok} "
            f"urls_enqueued={report.sitemap_urls_enqueued}"
        )
        print(
            f"coverage: status={report.coverage_status} "
            f"complete_within_approved_scope={report.complete_within_approved_scope} "
            f"depth_capped={report.depth_capped_expansions}"
        )
        if report.per_source:
            print("per-source:")
            for src in report.per_source:
                print(
                    f"  - id={src.source_id} status={src.status} "
                    f"fetches={src.fetches_attempted} "
                    f"extract_ok={src.extractions_success} "
                    f"compare_u={src.comparisons_unchanged}/"
                    f"c={src.comparisons_changed}"
                )
    if report.limits_reached:
        print("limits reached:", ", ".join(report.limits_reached))
    if report.failures:
        print("failures:")
        for fail in report.failures[:20]:
            print(
                f"  - [{fail.stage}] {fail.reason}"
                + (f" source={fail.source_id}" if fail.source_id else "")
                + (f" document={fail.document_id}" if fail.document_id else "")
            )
        if len(report.failures) > 20:
            print(f"  … {len(report.failures) - 20} more")
    for note in report.notes:
        print(f"note: {note}")
    if report.report_path:
        print(f"report_path: {report.report_path}")
    print(
        f"completed={report.completed} partial={report.partial} "
        f"stopped_due_to_limits={report.stopped_due_to_limits} "
        f"started={report.started_at} ended={report.ended_at}"
    )
    return 0 if not report.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
