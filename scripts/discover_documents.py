"""Bounded PDF/DOCX discovery from stored HTML (dry-run by default).

Lists in-scope binary document links. With ``--persist``, downloads and stores
PDF/DOCX only (no text extraction).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.crawl.documents_discover import discover_binaries_for_source  # noqa: E402
from lifepilot.crawl.policy import fetcher_policy  # noqa: E402
from lifepilot.db.session import get_session_factory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Discover in-allowlist PDF/DOCX links from stored HTML. "
            "Defaults to dry-run. Use --persist to download and store."
        )
    )
    parser.add_argument(
        "--source-id",
        type=int,
        required=True,
        help="Approved source whose allowlist governs discovery/fetch.",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Download and store in-scope PDF/DOCX documents.",
    )
    parser.add_argument(
        "--max-documents",
        type=int,
        default=3,
        help="Max binary downloads (or dry-run candidates) this run (default: 3).",
    )
    parser.add_argument(
        "--seed-html-only",
        action="store_true",
        help="Scan only the source seed HTML document (not other discovered HTML).",
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
        help="Print out-of-scope PDF/DOCX-looking links for manual review.",
    )
    args = parser.parse_args()
    if args.max_documents < 1:
        raise SystemExit("--max-documents must be >= 1")

    policy = fetcher_policy(max_discovered_binary_fetches_per_run=args.max_documents)
    factory = get_session_factory()
    with factory() as session:
        report = discover_binaries_for_source(
            session,
            args.source_id,
            persist=args.persist,
            policy=policy,
            storage_root=args.data_dir,
            include_discovered_html=not args.seed_html_only,
        )
        if args.persist:
            session.commit()
        else:
            session.rollback()

    mode = "persist" if args.persist else "dry-run"
    print(f"LifePilot binary document discovery ({mode})")
    print(f"user_agent: {policy.user_agent}")
    print(f"crawl_source_id={report.crawl_source_id}")
    print(f"parent_document_ids={report.parent_document_ids}")
    if report.message:
        print(f"note: {report.message}")
    print(
        f"links: in_scope_pdf={len(report.in_scope_pdf)} "
        f"in_scope_docx={len(report.in_scope_docx)} "
        f"out_of_scope_docs={len(report.out_of_scope_documents)} "
        f"listed={len(report.links)} "
        f"(list_cap={policy.max_discovered_links_listed})"
    )
    print(
        f"binary fetch cap this run: {policy.max_discovered_binary_fetches_per_run}"
    )

    print("in-scope PDF:")
    for link in report.in_scope_pdf:
        print(f"  - {link.url_normalized}")
    print("in-scope DOCX:")
    for link in report.in_scope_docx:
        print(f"  - {link.url_normalized}")

    if args.show_out_of_scope:
        print("out-of-scope PDF/DOCX (not fetched):")
        for link in report.out_of_scope_documents:
            print(f"  - {link.url_normalized}")

    failed = 0
    if report.fetch_results:
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

    if not report.parent_document_ids and "not found" in (report.message or ""):
        return 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
