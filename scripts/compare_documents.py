"""Compare meaningful extracted content across document versions (dry-run by default).

Does not crawl, download, or extract. Requires successful compatible extractions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.db.session import get_session_factory  # noqa: E402
from lifepilot.extract.compare import (  # noqa: E402
    COMPARISON_ALGORITHM_VERSION,
    STATUS_CHANGED,
    STATUS_UNAVAILABLE,
)
from lifepilot.extract.compare_runner import (  # noqa: E402
    compare_batch,
    persist_batch,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare structured extractions across document versions. "
            "Defaults to dry-run. Use --persist to store comparison JSON + DB rows. "
            "Does not crawl, download, or extract."
        )
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Write comparison JSON under data/compared/ and insert DB rows.",
    )
    parser.add_argument(
        "--document-id",
        type=int,
        default=None,
        help="Compare the latest two versions of this document.",
    )
    parser.add_argument(
        "--previous-version-id",
        type=int,
        default=None,
        help="Explicit previous document_versions.id (use with --current-version-id).",
    )
    parser.add_argument(
        "--current-version-id",
        type=int,
        default=None,
        help="Explicit current document_versions.id (use with --previous-version-id).",
    )
    parser.add_argument(
        "--max-documents",
        type=int,
        default=10,
        help="Max documents for batch mode (default: 10).",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Override storage root (default: ./data or LIFEPILOT_DATA_DIR).",
    )
    args = parser.parse_args()
    if args.max_documents < 1:
        raise SystemExit("--max-documents must be >= 1")

    factory = get_session_factory()
    with factory() as session:
        report = compare_batch(
            session,
            document_id=args.document_id,
            previous_version_id=args.previous_version_id,
            current_version_id=args.current_version_id,
            max_documents=args.max_documents,
            storage_root=args.data_dir,
        )
        if args.persist:
            report = persist_batch(
                session,
                report,
                storage_root=args.data_dir,
                commit_each=True,
            )
        else:
            session.rollback()

    mode = "persist" if args.persist else "dry-run"
    print(f"LifePilot content comparison ({mode})")
    print(f"algorithm={COMPARISON_ALGORITHM_VERSION}")
    if report.message:
        print(f"note: {report.message}")
    print(
        f"results: total={len(report.results)} "
        f"unchanged={report.unchanged_count} "
        f"changed={report.changed_count} "
        f"unavailable={report.unavailable_count}"
    )
    for item in report.results:
        extras = [f"document_id={item.document_id}"]
        if item.previous_version_id is not None:
            extras.append(
                f"prev=v{item.previous_version_number}({item.previous_version_id})"
            )
        if item.current_version_id is not None:
            extras.append(
                f"curr=v{item.current_version_number}({item.current_version_id})"
            )
        if item.extractor_name:
            extras.append(
                f"extractor={item.extractor_name}@{item.extractor_version}"
            )
        if item.raw_sha256_equal is not None:
            extras.append(f"raw_equal={item.raw_sha256_equal}")
        if item.layout_only:
            extras.append("layout_only=true")
        if item.comparison_id is not None:
            extras.append(f"comparison_id={item.comparison_id}")
        if item.storage_path:
            extras.append(f"path={item.storage_path}")
        print(f"  - [{item.status}] {', '.join(extras)}")
        print(f"      {item.summary_line()}")
        if item.message and item.message != item.summary_line():
            print(f"      {item.message}")
        if item.status == STATUS_CHANGED and item.diff:
            for change in (item.diff.get("changes") or [])[:8]:
                section = change.get("section_heading") or "(no section)"
                print(
                    f"      · {change['op']} {change['block_type']} "
                    f"@ {section}"
                )
            remaining = len(item.diff.get("changes") or []) - 8
            if remaining > 0:
                print(f"      · … {remaining} more change(s)")
        if item.status == STATUS_UNAVAILABLE and item.unavailable_reason:
            print(f"      reason: {item.unavailable_reason}")

    if report.message.startswith("no documents") and not report.results:
        return 1
    if report.message.startswith("both --previous"):
        return 1
    return 0 if report.unavailable_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
