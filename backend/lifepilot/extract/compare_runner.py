"""Orchestrate version pair selection, load extractions, compare, optionally persist."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lifepilot.crawl.storage import (
    comparison_file_path,
    data_root,
    delete_quiet,
    relative_storage_path,
    safe_read_bytes,
    write_bytes_atomic,
)
from lifepilot.db.models import (
    Document,
    DocumentContentComparison,
    DocumentExtraction,
    DocumentVersion,
)
from lifepilot.extract.compare import (
    COMPARISON_ALGORITHM_VERSION,
    STATUS_UNAVAILABLE,
    STATUS_UNCHANGED,
    ComparisonBatchReport,
    ComparisonResult,
    diff_structured_documents,
)
from lifepilot.extract.html_structured import structured_document_from_dict
from lifepilot.extract.runner import EXTRACTORS
from lifepilot.extract.schema import SCHEMA_VERSION

_COMPARABLE_EXTRACTION_STATUSES = frozenset({"success"})


def _extractor_for_kind(content_kind: str) -> tuple[str, str] | None:
    spec = EXTRACTORS.get(content_kind)
    if spec is None:
        return None
    return spec.name, spec.version


def _load_extraction_row(
    session: Session, version_id: int, extractor_name: str
) -> DocumentExtraction | None:
    return session.scalar(
        select(DocumentExtraction).where(
            DocumentExtraction.document_version_id == version_id,
            DocumentExtraction.extractor_name == extractor_name,
        )
    )


def _unavailable(
    *,
    document_id: int,
    previous_version_id: int | None,
    current_version_id: int | None,
    reason: str,
    extractor_name: str | None = None,
    extractor_version: str | None = None,
    previous_version_number: int | None = None,
    current_version_number: int | None = None,
    previous_content_sha256: str | None = None,
    current_content_sha256: str | None = None,
    raw_sha256_equal: bool | None = None,
) -> ComparisonResult:
    return ComparisonResult(
        document_id=document_id,
        previous_version_id=previous_version_id,
        current_version_id=current_version_id,
        status=STATUS_UNAVAILABLE,
        extractor_name=extractor_name,
        extractor_version=extractor_version,
        previous_version_number=previous_version_number,
        current_version_number=current_version_number,
        previous_content_sha256=previous_content_sha256,
        current_content_sha256=current_content_sha256,
        raw_sha256_equal=raw_sha256_equal,
        unavailable_reason=reason,
        message=reason,
    )


def _load_structured(
    root: Path, extraction: DocumentExtraction
) -> tuple[Any, str | None]:
    if not extraction.storage_path:
        return None, "extraction has no storage_path"
    try:
        raw = safe_read_bytes(root, extraction.storage_path)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"cannot read extraction JSON: {exc}"
    try:
        doc = structured_document_from_dict(data)
    except Exception as exc:  # noqa: BLE001
        return None, f"cannot parse extraction JSON: {exc}"
    return doc, None


def compare_version_pair(
    session: Session,
    *,
    document_id: int | None = None,
    previous_version_id: int,
    current_version_id: int,
    storage_root: Path | str | None = None,
) -> ComparisonResult:
    """Compare two document versions using successful compatible extractions."""
    root = data_root(storage_root)
    prev_v = session.get(DocumentVersion, previous_version_id)
    curr_v = session.get(DocumentVersion, current_version_id)
    if prev_v is None or curr_v is None:
        return _unavailable(
            document_id=document_id or 0,
            previous_version_id=previous_version_id,
            current_version_id=current_version_id,
            reason="one or both document versions not found",
        )
    if prev_v.document_id != curr_v.document_id:
        return _unavailable(
            document_id=prev_v.document_id,
            previous_version_id=previous_version_id,
            current_version_id=current_version_id,
            previous_version_number=prev_v.version_number,
            current_version_number=curr_v.version_number,
            reason=(
                f"versions belong to different documents "
                f"({prev_v.document_id} vs {curr_v.document_id})"
            ),
        )
    if document_id is not None and prev_v.document_id != document_id:
        return _unavailable(
            document_id=document_id,
            previous_version_id=previous_version_id,
            current_version_id=current_version_id,
            reason=(
                f"versions are for document_id={prev_v.document_id}, "
                f"not {document_id}"
            ),
        )

    document = session.get(Document, prev_v.document_id)
    if document is None:
        return _unavailable(
            document_id=prev_v.document_id,
            previous_version_id=previous_version_id,
            current_version_id=current_version_id,
            reason="document not found",
        )

    raw_equal = prev_v.content_sha256 == curr_v.content_sha256
    extractor = _extractor_for_kind(document.content_kind)
    if extractor is None:
        return _unavailable(
            document_id=document.id,
            previous_version_id=previous_version_id,
            current_version_id=current_version_id,
            previous_version_number=prev_v.version_number,
            current_version_number=curr_v.version_number,
            raw_sha256_equal=raw_equal,
            reason=f"unsupported content_kind={document.content_kind!r}",
        )
    extractor_name, expected_version = extractor

    if prev_v.id == curr_v.id:
        return _unavailable(
            document_id=document.id,
            previous_version_id=previous_version_id,
            current_version_id=current_version_id,
            extractor_name=extractor_name,
            extractor_version=expected_version,
            previous_version_number=prev_v.version_number,
            current_version_number=curr_v.version_number,
            raw_sha256_equal=True,
            reason="previous and current version ids are identical",
        )

    # Order by version_number so "previous" is always the older snapshot.
    if prev_v.version_number > curr_v.version_number:
        prev_v, curr_v = curr_v, prev_v

    prev_ex = _load_extraction_row(session, prev_v.id, extractor_name)
    curr_ex = _load_extraction_row(session, curr_v.id, extractor_name)

    def _missing_msg(label: str, version: DocumentVersion, row: DocumentExtraction | None) -> str:
        if row is None:
            return (
                f"{label} version_id={version.id} (v{version.version_number}) "
                f"has no {extractor_name} extraction; run extract_documents.py"
            )
        if row.status not in _COMPARABLE_EXTRACTION_STATUSES:
            return (
                f"{label} version_id={version.id} (v{version.version_number}) "
                f"extraction status={row.status!r} (need success); "
                f"re-extract if appropriate"
            )
        if row.extractor_version != expected_version:
            return (
                f"{label} version_id={version.id} (v{version.version_number}) "
                f"extractor_version={row.extractor_version!r} incompatible with "
                f"required {extractor_name}@{expected_version}; re-extract"
            )
        if not row.storage_path:
            return (
                f"{label} version_id={version.id} (v{version.version_number}) "
                f"successful extraction missing storage_path; re-extract"
            )
        if not row.content_sha256:
            return (
                f"{label} version_id={version.id} (v{version.version_number}) "
                f"successful extraction missing content_sha256; re-extract"
            )
        return ""

    for label, version, row in (
        ("previous", prev_v, prev_ex),
        ("current", curr_v, curr_ex),
    ):
        reason = _missing_msg(label, version, row)
        if reason:
            return _unavailable(
                document_id=document.id,
                previous_version_id=prev_v.id,
                current_version_id=curr_v.id,
                extractor_name=extractor_name,
                extractor_version=expected_version,
                previous_version_number=prev_v.version_number,
                current_version_number=curr_v.version_number,
                previous_content_sha256=prev_ex.content_sha256 if prev_ex else None,
                current_content_sha256=curr_ex.content_sha256 if curr_ex else None,
                raw_sha256_equal=raw_equal,
                reason=reason,
            )

    assert prev_ex is not None and curr_ex is not None
    if prev_ex.extractor_version != curr_ex.extractor_version:
        return _unavailable(
            document_id=document.id,
            previous_version_id=prev_v.id,
            current_version_id=curr_v.id,
            extractor_name=extractor_name,
            extractor_version=expected_version,
            previous_version_number=prev_v.version_number,
            current_version_number=curr_v.version_number,
            previous_content_sha256=prev_ex.content_sha256,
            current_content_sha256=curr_ex.content_sha256,
            raw_sha256_equal=raw_equal,
            reason=(
                f"extractor versions differ "
                f"({prev_ex.extractor_version} vs {curr_ex.extractor_version}); "
                f"re-extract both with {extractor_name}@{expected_version}"
            ),
        )

    prev_doc, prev_err = _load_structured(root, prev_ex)
    if prev_err:
        return _unavailable(
            document_id=document.id,
            previous_version_id=prev_v.id,
            current_version_id=curr_v.id,
            extractor_name=extractor_name,
            extractor_version=prev_ex.extractor_version,
            previous_version_number=prev_v.version_number,
            current_version_number=curr_v.version_number,
            previous_content_sha256=prev_ex.content_sha256,
            current_content_sha256=curr_ex.content_sha256,
            raw_sha256_equal=raw_equal,
            reason=f"previous: {prev_err}",
        )
    curr_doc, curr_err = _load_structured(root, curr_ex)
    if curr_err:
        return _unavailable(
            document_id=document.id,
            previous_version_id=prev_v.id,
            current_version_id=curr_v.id,
            extractor_name=extractor_name,
            extractor_version=curr_ex.extractor_version,
            previous_version_number=prev_v.version_number,
            current_version_number=curr_v.version_number,
            previous_content_sha256=prev_ex.content_sha256,
            current_content_sha256=curr_ex.content_sha256,
            raw_sha256_equal=raw_equal,
            reason=f"current: {curr_err}",
        )

    # Schema version from stored JSON (already loaded via bytes); re-read envelope.
    try:
        prev_meta = json.loads(safe_read_bytes(root, prev_ex.storage_path).decode("utf-8"))
        curr_meta = json.loads(safe_read_bytes(root, curr_ex.storage_path).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return _unavailable(
            document_id=document.id,
            previous_version_id=prev_v.id,
            current_version_id=curr_v.id,
            extractor_name=extractor_name,
            extractor_version=curr_ex.extractor_version,
            previous_version_number=prev_v.version_number,
            current_version_number=curr_v.version_number,
            reason=f"cannot inspect schema_version: {exc}",
        )
    prev_schema = int(prev_meta.get("schema_version") or 0)
    curr_schema = int(curr_meta.get("schema_version") or 0)
    if prev_schema != curr_schema:
        return _unavailable(
            document_id=document.id,
            previous_version_id=prev_v.id,
            current_version_id=curr_v.id,
            extractor_name=extractor_name,
            extractor_version=curr_ex.extractor_version,
            previous_version_number=prev_v.version_number,
            current_version_number=curr_v.version_number,
            previous_content_sha256=prev_ex.content_sha256,
            current_content_sha256=curr_ex.content_sha256,
            raw_sha256_equal=raw_equal,
            reason=(
                f"schema_version mismatch ({prev_schema} vs {curr_schema}); "
                f"re-extract both to schema {SCHEMA_VERSION}"
            ),
        )
    if prev_schema != SCHEMA_VERSION:
        return _unavailable(
            document_id=document.id,
            previous_version_id=prev_v.id,
            current_version_id=curr_v.id,
            extractor_name=extractor_name,
            extractor_version=curr_ex.extractor_version,
            previous_version_number=prev_v.version_number,
            current_version_number=curr_v.version_number,
            previous_content_sha256=prev_ex.content_sha256,
            current_content_sha256=curr_ex.content_sha256,
            raw_sha256_equal=raw_equal,
            reason=(
                f"schema_version={prev_schema} incompatible with "
                f"comparator expecting {SCHEMA_VERSION}; re-extract"
            ),
        )

    # Fast path: stored content hashes equal → unchanged (still note raw/layout).
    if prev_ex.content_sha256 == curr_ex.content_sha256:
        diff = {
            "algorithm_version": COMPARISON_ALGORITHM_VERSION,
            "schema_version": SCHEMA_VERSION,
            "status": STATUS_UNCHANGED,
            "title_changed": False,
            "previous_title": prev_doc.title,
            "current_title": curr_doc.title,
            "layout_only": False,
            "layout_differences": [],
            "changes": [],
            "summary": {
                "added": 0,
                "removed": 0,
                "modified": 0,
                "layout_differences": 0,
            },
            "previous_content_sha256": prev_ex.content_sha256,
            "current_content_sha256": curr_ex.content_sha256,
            "hash_equal": True,
        }
        return ComparisonResult(
            document_id=document.id,
            previous_version_id=prev_v.id,
            current_version_id=curr_v.id,
            status=STATUS_UNCHANGED,
            extractor_name=extractor_name,
            extractor_version=curr_ex.extractor_version,
            previous_content_sha256=prev_ex.content_sha256,
            current_content_sha256=curr_ex.content_sha256,
            previous_version_number=prev_v.version_number,
            current_version_number=curr_v.version_number,
            raw_sha256_equal=raw_equal,
            layout_only=False,
            diff=diff,
            message=(
                "extracted content hashes identical"
                + ("; raw bytes also identical" if raw_equal else "; raw bytes differ")
            ),
        )

    diff = diff_structured_documents(prev_doc, curr_doc)
    diff["document_id"] = document.id
    diff["previous_version_id"] = prev_v.id
    diff["current_version_id"] = curr_v.id
    diff["previous_version_number"] = prev_v.version_number
    diff["current_version_number"] = curr_v.version_number
    diff["extractor_name"] = extractor_name
    diff["extractor_version"] = curr_ex.extractor_version
    diff["raw_sha256_equal"] = raw_equal
    # Prefer DB extraction hashes for persistence identity.
    diff["previous_content_sha256"] = prev_ex.content_sha256
    diff["current_content_sha256"] = curr_ex.content_sha256

    status = diff["status"]
    layout_only = bool(diff.get("layout_only"))
    if status == STATUS_UNCHANGED and layout_only:
        message = (
            "textual content identical; layout-only differences "
            "(e.g. PDF page metadata)"
        )
    elif status == STATUS_UNCHANGED:
        message = "extracted textual content identical"
    else:
        message = ComparisonResult(
            document_id=document.id,
            previous_version_id=prev_v.id,
            current_version_id=curr_v.id,
            status=status,
            diff=diff,
        ).summary_line()

    return ComparisonResult(
        document_id=document.id,
        previous_version_id=prev_v.id,
        current_version_id=curr_v.id,
        status=status,
        extractor_name=extractor_name,
        extractor_version=curr_ex.extractor_version,
        previous_content_sha256=prev_ex.content_sha256,
        current_content_sha256=curr_ex.content_sha256,
        previous_version_number=prev_v.version_number,
        current_version_number=curr_v.version_number,
        raw_sha256_equal=raw_equal,
        layout_only=layout_only,
        diff=diff,
        message=message,
    )


def latest_two_versions(
    session: Session, document_id: int
) -> tuple[DocumentVersion, DocumentVersion] | None:
    versions = list(
        session.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version_number.desc())
            .limit(2)
        ).all()
    )
    if len(versions) < 2:
        return None
    current, previous = versions[0], versions[1]
    return previous, current


def compare_document_latest(
    session: Session,
    document_id: int,
    *,
    storage_root: Path | str | None = None,
) -> ComparisonResult:
    pair = latest_two_versions(session, document_id)
    if pair is None:
        document = session.get(Document, document_id)
        return _unavailable(
            document_id=document_id,
            previous_version_id=None,
            current_version_id=None,
            reason=(
                "document not found"
                if document is None
                else "document has fewer than two versions"
            ),
        )
    previous, current = pair
    return compare_version_pair(
        session,
        document_id=document_id,
        previous_version_id=previous.id,
        current_version_id=current.id,
        storage_root=storage_root,
    )


def compare_batch(
    session: Session,
    *,
    document_id: int | None = None,
    previous_version_id: int | None = None,
    current_version_id: int | None = None,
    max_documents: int = 10,
    storage_root: Path | str | None = None,
) -> ComparisonBatchReport:
    """Compare explicit pair, one document's latest two, or a bounded batch."""
    report = ComparisonBatchReport()
    if max_documents < 1:
        raise ValueError("max_documents must be >= 1")

    if previous_version_id is not None or current_version_id is not None:
        if previous_version_id is None or current_version_id is None:
            report.message = "both --previous-version-id and --current-version-id required"
            return report
        report.results.append(
            compare_version_pair(
                session,
                document_id=document_id,
                previous_version_id=previous_version_id,
                current_version_id=current_version_id,
                storage_root=storage_root,
            )
        )
        return report

    if document_id is not None:
        report.results.append(
            compare_document_latest(
                session, document_id, storage_root=storage_root
            )
        )
        return report

    # Bounded batch: documents with >= 2 versions, ordered by id.
    doc_ids = list(
        session.scalars(
            select(DocumentVersion.document_id)
            .group_by(DocumentVersion.document_id)
            .having(func.count(DocumentVersion.id) >= 2)
            .order_by(DocumentVersion.document_id.asc())
            .limit(max_documents)
        ).all()
    )
    if not doc_ids:
        report.message = "no documents with at least two versions"
        return report
    for did in doc_ids:
        report.results.append(
            compare_document_latest(session, did, storage_root=storage_root)
        )
    return report


def _existing_comparison(
    session: Session,
    *,
    previous_version_id: int,
    current_version_id: int,
    extractor_name: str,
    extractor_version: str,
    algorithm_version: str,
) -> DocumentContentComparison | None:
    return session.scalar(
        select(DocumentContentComparison).where(
            DocumentContentComparison.previous_version_id == previous_version_id,
            DocumentContentComparison.current_version_id == current_version_id,
            DocumentContentComparison.extractor_name == extractor_name,
            DocumentContentComparison.extractor_version == extractor_version,
            DocumentContentComparison.algorithm_version == algorithm_version,
        )
    )


def persist_comparison(
    session: Session,
    result: ComparisonResult,
    *,
    storage_root: Path | str | None = None,
) -> ComparisonResult:
    """Persist one comparison result. Idempotent for identical inputs/outcome.

    Never silently overwrites a historical row whose hashes/status differ.
    """
    if result.previous_version_id is None or result.current_version_id is None:
        result.message = "cannot persist: missing version ids"
        return result
    if not result.extractor_name or not result.extractor_version:
        result.message = "cannot persist: missing extractor identity"
        return result

    root = data_root(storage_root)
    existing = _existing_comparison(
        session,
        previous_version_id=result.previous_version_id,
        current_version_id=result.current_version_id,
        extractor_name=result.extractor_name,
        extractor_version=result.extractor_version,
        algorithm_version=result.algorithm_version,
    )
    if existing is not None:
        same = (
            existing.status == result.status
            and existing.previous_content_sha256 == result.previous_content_sha256
            and existing.current_content_sha256 == result.current_content_sha256
            and existing.unavailable_reason == result.unavailable_reason
        )
        if same:
            result.comparison_id = existing.id
            result.storage_path = existing.storage_path
            result.message = (result.message or "comparison unchanged") + " (existing row)"
            return result
        result.status = STATUS_UNAVAILABLE
        result.unavailable_reason = (
            f"historical comparison_id={existing.id} exists with different "
            f"status/hashes; not overwritten"
        )
        result.message = result.unavailable_reason
        result.comparison_id = existing.id
        result.storage_path = existing.storage_path
        return result

    payload = {
        "algorithm_version": result.algorithm_version,
        "document_id": result.document_id,
        "previous_version_id": result.previous_version_id,
        "current_version_id": result.current_version_id,
        "previous_version_number": result.previous_version_number,
        "current_version_number": result.current_version_number,
        "extractor_name": result.extractor_name,
        "extractor_version": result.extractor_version,
        "status": result.status,
        "previous_content_sha256": result.previous_content_sha256,
        "current_content_sha256": result.current_content_sha256,
        "raw_sha256_equal": result.raw_sha256_equal,
        "layout_only": result.layout_only,
        "unavailable_reason": result.unavailable_reason,
        "message": result.message,
        "diff": result.diff,
        "compared_at": datetime.now(timezone.utc).isoformat(),
    }
    abs_path = comparison_file_path(
        root,
        result.document_id,
        result.previous_version_number or 0,
        result.current_version_number or 0,
        extractor_name=result.extractor_name,
        algorithm_version=result.algorithm_version,
    )
    rel_path = relative_storage_path(root, abs_path)
    write_bytes_atomic(
        abs_path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        ),
    )
    try:
        row = DocumentContentComparison(
            document_id=result.document_id,
            previous_version_id=result.previous_version_id,
            current_version_id=result.current_version_id,
            extractor_name=result.extractor_name,
            extractor_version=result.extractor_version,
            algorithm_version=result.algorithm_version,
            status=result.status,
            previous_content_sha256=result.previous_content_sha256,
            current_content_sha256=result.current_content_sha256,
            storage_path=rel_path,
            unavailable_reason=result.unavailable_reason,
            compared_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
    except Exception:
        delete_quiet(abs_path)
        raise

    result.comparison_id = row.id
    result.storage_path = rel_path
    return result


def persist_batch(
    session: Session,
    report: ComparisonBatchReport,
    *,
    storage_root: Path | str | None = None,
    commit_each: bool = False,
) -> ComparisonBatchReport:
    out = ComparisonBatchReport(message=report.message)
    root = data_root(storage_root)
    for item in report.results:
        persisted = persist_comparison(session, item, storage_root=root)
        if commit_each:
            try:
                session.commit()
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                if persisted.storage_path:
                    delete_quiet(root / persisted.storage_path)
                persisted = ComparisonResult(
                    document_id=item.document_id,
                    previous_version_id=item.previous_version_id,
                    current_version_id=item.current_version_id,
                    status=STATUS_UNAVAILABLE,
                    extractor_name=item.extractor_name,
                    extractor_version=item.extractor_version,
                    unavailable_reason=f"commit failed after persist: {exc}",
                    message=f"commit failed after persist: {exc}",
                )
        out.results.append(persisted)
    return out
