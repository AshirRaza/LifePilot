"""Structured extraction from stored HTML/PDF/DOCX versions (dry-run by default).

Reads existing raw files only. Does not crawl or download. Use ``--persist`` to
write derived JSON under ``data/extracted/`` and upsert ``document_extractions``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.db.session import get_session_factory  # noqa: E402
from lifepilot.extract.docx_structured import (  # noqa: E402
    EXTRACTOR_NAME as DOCX_NAME,
)
from lifepilot.extract.docx_structured import (  # noqa: E402
    EXTRACTOR_VERSION as DOCX_VERSION,
)
from lifepilot.extract.html_structured import (  # noqa: E402
    EXTRACTOR_NAME as HTML_NAME,
)
from lifepilot.extract.html_structured import (  # noqa: E402
    EXTRACTOR_VERSION as HTML_VERSION,
)
from lifepilot.extract.pdf_structured import (  # noqa: E402
    EXTRACTOR_NAME as PDF_NAME,
)
from lifepilot.extract.pdf_structured import (  # noqa: E402
    EXTRACTOR_VERSION as PDF_VERSION,
)
from lifepilot.extract.runner import extract_batch  # noqa: E402


def _parse_kinds(args: argparse.Namespace) -> list[str]:
    kinds: list[str] = []
    if args.html:
        kinds.append("html")
    if args.pdf:
        kinds.append("pdf")
    if args.docx:
        kinds.append("docx")
    if args.kinds:
        for part in args.kinds.split(","):
            part = part.strip()
            if part:
                kinds.append(part)
    return kinds


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract structured text from stored HTML/PDF/DOCX document versions. "
            "Defaults to dry-run. Use --persist to write extraction JSON + DB rows."
        )
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Write derived JSON and upsert document_extractions rows.",
    )
    parser.add_argument(
        "--document-id",
        type=int,
        default=None,
        help="Limit to one documents.id (current version by default).",
    )
    parser.add_argument(
        "--version-id",
        type=int,
        default=None,
        help="Extract a specific document_versions.id.",
    )
    parser.add_argument(
        "--max-documents",
        type=int,
        default=10,
        help="Max distinct documents this run (default: 10).",
    )
    parser.add_argument(
        "--all-versions",
        action="store_true",
        help="Include non-current versions (still bounded by --max-documents).",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Include HTML documents.",
    )
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="Include PDF documents.",
    )
    parser.add_argument(
        "--docx",
        action="store_true",
        help="Include DOCX documents.",
    )
    parser.add_argument(
        "--kinds",
        type=str,
        default=None,
        help="Comma-separated kinds: html,pdf,docx (default: all when flags omitted).",
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

    kinds = _parse_kinds(args) or None

    factory = get_session_factory()
    with factory() as session:
        report = extract_batch(
            session,
            persist=args.persist,
            document_id=args.document_id,
            version_id=args.version_id,
            max_documents=args.max_documents,
            include_non_current=args.all_versions,
            storage_root=args.data_dir,
            kinds=kinds,
            commit_each=args.persist,
        )
        if not args.persist:
            session.rollback()

    mode = "persist" if args.persist else "dry-run"
    print(f"LifePilot document extraction ({mode})")
    print(
        f"extractors: {HTML_NAME}@{HTML_VERSION}, "
        f"{PDF_NAME}@{PDF_VERSION}, {DOCX_NAME}@{DOCX_VERSION}"
    )
    if kinds:
        print(f"kinds={','.join(kinds)}")
    else:
        print("kinds=html,pdf,docx")
    if report.message:
        print(f"note: {report.message}")
    print(
        f"results: total={len(report.results)} "
        f"dry_run={report.dry_run_count} "
        f"success={report.success_count} "
        f"empty={report.empty_count} "
        f"unchanged={report.unchanged_count} "
        f"skipped={report.skipped_count} "
        f"failed={report.failed_count}"
    )
    for item in report.results:
        extras = [
            f"document_id={item.document_id}",
            f"version_id={item.document_version_id}",
            f"v{item.version_number}",
        ]
        if item.content_kind:
            extras.append(f"kind={item.content_kind}")
        if item.extractor_name:
            extras.append(f"extractor={item.extractor_name}@{item.extractor_version}")
        if item.block_count:
            extras.append(f"blocks={item.block_count}")
        if item.content_sha256:
            extras.append(f"sha256={item.content_sha256[:12]}…")
        if item.storage_path:
            extras.append(f"path={item.storage_path}")
        if item.extraction_id is not None:
            extras.append(f"extraction_id={item.extraction_id}")
        print(f"  - [{item.status}] {', '.join(extras)}")
        if item.title:
            print(f"      title={item.title!r}")
        if item.message:
            print(f"      {item.message}")

    if report.message.startswith("no matching") and not report.results:
        return 1
    return 0 if report.failed_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
