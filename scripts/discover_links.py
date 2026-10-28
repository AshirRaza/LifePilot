"""Bounded link discovery from stored seed HTML (dry-run by default).

Lists in-scope HTML/PDF links from a stored document. With ``--persist``,
fetches in-scope HTML only (PDFs are reported, not downloaded).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.crawl.discover import (  # noqa: E402
    discover_from_document,
    discover_from_source_seed,
)
from lifepilot.crawl.policy import fetcher_policy  # noqa: E402
from lifepilot.db.session import get_session_factory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Discover in-allowlist links from stored HTML. "
            "Defaults to dry-run listing. "
            "Use --persist to fetch in-scope HTML pages only."
        )
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--source-id",
        type=int,
        help="Discover from the stored seed document of this approved source.",
    )
    target.add_argument(
        "--document-id",
        type=int,
        help="Discover from this stored document's current HTML version.",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Fetch and store in-scope HTML links (PDFs are not downloaded).",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Override storage root (default: ./data or LIFEPILOT_DATA_DIR).",
    )
    parser.add_argument(
        "--show-out-of-scope",
        action="store_true",
        help="Also print out-of-scope and invalid links (capped by policy).",
    )
    args = parser.parse_args()

    policy = fetcher_policy()
    factory = get_session_factory()
    with factory() as session:
        if args.document_id is not None:
            report = discover_from_document(
                session,
                args.document_id,
                persist=args.persist,
                policy=policy,
                storage_root=args.data_dir,
                commit_each=args.persist,
            )
        else:
            report = discover_from_source_seed(
                session,
                args.source_id,
                persist=args.persist,
                policy=policy,
                storage_root=args.data_dir,
                commit_each=args.persist,
            )
        if not args.persist:
            session.rollback()

    mode = "persist" if args.persist else "dry-run"
    print(f"LifePilot link discovery ({mode})")
    print(f"user_agent: {policy.user_agent}")
    print(
        f"parent document_id={report.parent_document_id} "
        f"source_id={report.source_id}"
    )
    print(f"base_url: {report.base_url}")
    if report.message:
        print(f"note: {report.message}")

    html_n = len(report.in_scope_html)
    pdf_n = len(report.in_scope_pdf)
    out_n = len(report.out_of_scope)
    print(
        f"links: in_scope_html={html_n} in_scope_pdf={pdf_n} "
        f"out_of_scope={out_n} listed={len(report.links)} "
        f"(cap={policy.max_discovered_links_listed})"
    )
    print(
        f"html fetch cap this run: {policy.max_discovered_html_fetches_per_run}"
    )

    print("in-scope HTML:")
    for link in report.in_scope_html:
        print(f"  - {link.url_normalized}")

    print("in-scope PDF (not fetched in this phase):")
    for link in report.in_scope_pdf:
        print(f"  - {link.url_normalized}")

    if args.show_out_of_scope:
        print("out-of-scope / other / invalid:")
        for link in report.links:
            if link.kind in {"in_scope_html", "in_scope_pdf"}:
                continue
            print(f"  - [{link.kind}] {link.url_normalized or link.raw_href}")

    if report.fetch_results:
        failed = 0
        print("fetch results:")
        for item in report.fetch_results:
            extras = []
            if item.document_id is not None:
                extras.append(f"document_id={item.document_id}")
            if item.version_number is not None:
                extras.append(f"v{item.version_number}")
            if item.storage_path:
                extras.append(f"path={item.storage_path}")
            if item.http_status is not None:
                extras.append(f"http={item.http_status}")
            extra = f" ({', '.join(extras)})" if extras else ""
            print(f"  - [{item.status}] {item.seed_url}{extra}")
            if item.message:
                print(f"      {item.message}")
            if item.status == "failed":
                failed += 1
        return 0 if failed == 0 else 1

    return 0 if report.parent_document_id else 1


if __name__ == "__main__":
    raise SystemExit(main())
