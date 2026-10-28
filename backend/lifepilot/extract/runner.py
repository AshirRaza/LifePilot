"""Orchestrate structured extraction from stored document versions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifepilot.crawl.storage import (
    data_root,
    delete_quiet,
    extraction_file_path,
    relative_storage_path,
    safe_read_bytes,
    write_bytes_atomic,
)
from lifepilot.db.models import Document, DocumentExtraction, DocumentVersion
from lifepilot.extract import docx_structured, html_structured, pdf_structured
from lifepilot.extract.schema import StructuredDocument, normalized_content_sha256

STATUS_DRY_RUN = "dry_run"
STATUS_SUCCESS = "success"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_UNCHANGED = "unchanged"

KIND_HTML = "html_page"
KIND_PDF = "pdf"
KIND_DOCX = "docx"

_KIND_TO_SUFFIX = {
    KIND_HTML: ".html",
    KIND_PDF: ".pdf",
    KIND_DOCX: ".docx",
}


@dataclass(frozen=True)
class ExtractorSpec:
    name: str
    version: str
    content_kind: str
    extract: Callable[[bytes], StructuredDocument]


EXTRACTORS: dict[str, ExtractorSpec] = {
    KIND_HTML: ExtractorSpec(
        name=html_structured.EXTRACTOR_NAME,
        version=html_structured.EXTRACTOR_VERSION,
        content_kind=KIND_HTML,
        extract=html_structured.extract_html_document,
    ),
    KIND_PDF: ExtractorSpec(
        name=pdf_structured.EXTRACTOR_NAME,
        version=pdf_structured.EXTRACTOR_VERSION,
        content_kind=KIND_PDF,
        extract=pdf_structured.extract_pdf_document,
    ),
    KIND_DOCX: ExtractorSpec(
        name=docx_structured.EXTRACTOR_NAME,
        version=docx_structured.EXTRACTOR_VERSION,
        content_kind=KIND_DOCX,
        extract=docx_structured.extract_docx_document,
    ),
}


@dataclass
class ExtractionResult:
    document_id: int
    document_version_id: int
    version_number: int
    status: str
    title: str | None = None
    content_sha256: str | None = None
    storage_path: str | None = None
    extraction_id: int | None = None
    message: str = ""
    block_count: int = 0
    extractor_name: str | None = None
    extractor_version: str | None = None
    content_kind: str | None = None


@dataclass
class ExtractionBatchReport:
    results: list[ExtractionResult] = field(default_factory=list)
    message: str = ""

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_SUCCESS)

    @property
    def empty_count(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_EMPTY)

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_SKIPPED)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_FAILED)

    @property
    def unchanged_count(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_UNCHANGED)

    @property
    def dry_run_count(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_DRY_RUN)


def _existing_extraction(
    session: Session, document_version_id: int, extractor_name: str
) -> DocumentExtraction | None:
    return session.scalar(
        select(DocumentExtraction).where(
            DocumentExtraction.document_version_id == document_version_id,
            DocumentExtraction.extractor_name == extractor_name,
        )
    )


def _upsert_extraction(
    session: Session,
    *,
    existing: DocumentExtraction | None,
    document_version_id: int,
    extractor_name: str,
    extractor_version: str,
    status: str,
    title: str | None,
    content_sha256: str | None,
    storage_path: str | None,
    message: str | None,
) -> DocumentExtraction:
    now = datetime.now(timezone.utc)
    if existing is None:
        row = DocumentExtraction(
            document_version_id=document_version_id,
            extractor_name=extractor_name,
            extractor_version=extractor_version,
            status=status,
            content_sha256=content_sha256,
            storage_path=storage_path,
            title=title,
            message=message,
            extracted_at=now,
        )
        session.add(row)
        session.flush()
        return row

    # Provenance: one current row per (version, extractor_name). Version bumps
    # overwrite the derived JSON path and update extractor_version / hash /
    # extracted_at. Raw document_versions are never modified. Prior JSON may
    # remain on disk only if a different path was used previously.
    existing.extractor_version = extractor_version
    existing.status = status
    existing.content_sha256 = content_sha256
    existing.storage_path = storage_path
    existing.title = title
    existing.message = message
    existing.extracted_at = now
    session.flush()
    return existing


def _write_extraction_json(
    root: Path,
    *,
    document_id: int,
    version_number: int,
    document_version_id: int,
    source_url: str,
    document: StructuredDocument,
    content_sha256: str,
    spec: ExtractorSpec,
) -> str:
    abs_path = extraction_file_path(
        root,
        document_id,
        version_number,
        extractor_name=spec.name,
    )
    payload = document.to_storage_dict(
        extractor=spec.name,
        extractor_version=spec.version,
        document_id=document_id,
        document_version_id=document_version_id,
        source_url=source_url,
        content_sha256=content_sha256,
    )
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_bytes_atomic(abs_path, body.encode("utf-8"))
    return relative_storage_path(root, abs_path)


def extract_document_version(
    session: Session,
    version: DocumentVersion,
    document: Document,
    *,
    persist: bool = False,
    storage_root: Path | str | None = None,
) -> ExtractionResult:
    """Extract structured content for one stored document version."""
    base = ExtractionResult(
        document_id=document.id,
        document_version_id=version.id,
        version_number=version.version_number,
        status=STATUS_SKIPPED,
        content_kind=document.content_kind,
    )

    spec = EXTRACTORS.get(document.content_kind)
    if spec is not None:
        base.extractor_name = spec.name
        base.extractor_version = spec.version

    if document.discovery_status == "rejected":
        msg = "document discovery_status=rejected"
        extractor_name = spec.name if spec else "unknown"
        extractor_version = spec.version if spec else "0"
        if not persist:
            base.status = STATUS_DRY_RUN
            base.message = f"would skip: {msg}"
            return base
        existing = _existing_extraction(session, version.id, extractor_name)
        if (
            existing is not None
            and existing.status == STATUS_SKIPPED
            and existing.message == msg
            and existing.extractor_version == extractor_version
        ):
            base.status = STATUS_UNCHANGED
            base.extraction_id = existing.id
            base.message = msg
            return base
        row = _upsert_extraction(
            session,
            existing=existing,
            document_version_id=version.id,
            extractor_name=extractor_name,
            extractor_version=extractor_version,
            status=STATUS_SKIPPED,
            title=None,
            content_sha256=None,
            storage_path=None,
            message=msg,
        )
        base.status = STATUS_SKIPPED
        base.extraction_id = row.id
        base.message = msg
        return base

    if spec is None:
        msg = f"unsupported content_kind={document.content_kind!r}"
        if not persist:
            base.status = STATUS_DRY_RUN
            base.message = f"would skip: {msg}"
            return base
        base.status = STATUS_SKIPPED
        base.message = msg
        return base

    expected_suffix = _KIND_TO_SUFFIX[spec.content_kind]
    if not version.storage_path.endswith(expected_suffix):
        msg = (
            f"storage_path does not match {spec.content_kind}: "
            f"{version.storage_path}"
        )
        if not persist:
            base.status = STATUS_DRY_RUN
            base.message = f"would skip: {msg}"
            return base
        existing = _existing_extraction(session, version.id, spec.name)
        row = _upsert_extraction(
            session,
            existing=existing,
            document_version_id=version.id,
            extractor_name=spec.name,
            extractor_version=spec.version,
            status=STATUS_SKIPPED,
            title=None,
            content_sha256=None,
            storage_path=None,
            message=msg,
        )
        base.extraction_id = row.id
        base.status = STATUS_SKIPPED
        base.message = msg
        return base

    root = data_root(storage_root)
    try:
        raw = safe_read_bytes(root, version.storage_path)
        structured = spec.extract(raw)
        digest = normalized_content_sha256(structured)
    except Exception as exc:  # noqa: BLE001 — persist failure status
        msg = f"extraction failed: {exc}"
        if persist:
            existing = _existing_extraction(session, version.id, spec.name)
            row = _upsert_extraction(
                session,
                existing=existing,
                document_version_id=version.id,
                extractor_name=spec.name,
                extractor_version=spec.version,
                status=STATUS_FAILED,
                title=None,
                content_sha256=None,
                storage_path=None,
                message=msg,
            )
            base.extraction_id = row.id
            base.status = STATUS_FAILED
            base.message = msg
        else:
            base.status = STATUS_DRY_RUN
            base.message = f"would fail: {msg}"
        return base

    status = STATUS_SUCCESS if structured.blocks else STATUS_EMPTY
    base.title = structured.title or None
    base.content_sha256 = digest
    base.block_count = len(structured.blocks)

    if not persist:
        empty_note = ""
        if structured.empty_pages:
            empty_note = f" empty_pages={list(structured.empty_pages)}"
        base.status = STATUS_DRY_RUN
        base.message = (
            f"would extract {status}: title={structured.title!r} "
            f"blocks={len(structured.blocks)} sha256={digest[:12]}…"
            f"{empty_note}"
        )
        return base

    existing = _existing_extraction(session, version.id, spec.name)
    if (
        existing is not None
        and existing.status == status
        and existing.content_sha256 == digest
        and existing.extractor_version == spec.version
        and existing.storage_path
    ):
        try:
            safe_read_bytes(root, existing.storage_path)
            base.status = STATUS_UNCHANGED
            base.extraction_id = existing.id
            base.storage_path = existing.storage_path
            base.message = "extraction unchanged"
            return base
        except FileNotFoundError:
            pass

    previous_path = existing.storage_path if existing is not None else None
    rel_path = _write_extraction_json(
        root,
        document_id=document.id,
        version_number=version.version_number,
        document_version_id=version.id,
        source_url=version.source_url,
        document=structured,
        content_sha256=digest,
        spec=spec,
    )
    message = None
    if status == STATUS_EMPTY:
        message = "no content blocks extracted"
    elif structured.empty_pages:
        message = f"empty_pages={list(structured.empty_pages)}"
    try:
        row = _upsert_extraction(
            session,
            existing=existing,
            document_version_id=version.id,
            extractor_name=spec.name,
            extractor_version=spec.version,
            status=status,
            title=structured.title or None,
            content_sha256=digest,
            storage_path=rel_path,
            message=message,
        )
    except Exception:
        # Avoid orphan JSON when the DB write fails. Same-path overwrite
        # cannot restore prior bytes; leave the new file for retry.
        if previous_path != rel_path:
            delete_quiet(root / rel_path)
        raise
    base.status = status
    base.storage_path = rel_path
    base.extraction_id = row.id
    base.message = "extracted" if status == STATUS_SUCCESS else (message or "empty content")
    return base


def extract_html_version(
    session: Session,
    version: DocumentVersion,
    document: Document,
    *,
    persist: bool = False,
    storage_root: Path | str | None = None,
) -> ExtractionResult:
    """Back-compat wrapper for HTML-only extraction."""
    if document.content_kind != KIND_HTML:
        base = ExtractionResult(
            document_id=document.id,
            document_version_id=version.id,
            version_number=version.version_number,
            status=STATUS_DRY_RUN if not persist else STATUS_SKIPPED,
            content_kind=document.content_kind,
            extractor_name=html_structured.EXTRACTOR_NAME,
            extractor_version=html_structured.EXTRACTOR_VERSION,
            message=(
                f"would skip: content_kind={document.content_kind!r} is not html_page"
                if not persist
                else f"content_kind={document.content_kind!r} is not html_page"
            ),
        )
        if persist:
            existing = _existing_extraction(
                session, version.id, html_structured.EXTRACTOR_NAME
            )
            row = _upsert_extraction(
                session,
                existing=existing,
                document_version_id=version.id,
                extractor_name=html_structured.EXTRACTOR_NAME,
                extractor_version=html_structured.EXTRACTOR_VERSION,
                status=STATUS_SKIPPED,
                title=None,
                content_sha256=None,
                storage_path=None,
                message=base.message,
            )
            base.extraction_id = row.id
            base.status = STATUS_SKIPPED
        return base
    return extract_document_version(
        session,
        version,
        document,
        persist=persist,
        storage_root=storage_root,
    )


def _normalize_kinds(kinds: list[str] | tuple[str, ...] | None) -> list[str]:
    if not kinds:
        return [KIND_HTML, KIND_PDF, KIND_DOCX]
    normalized: list[str] = []
    alias = {
        "html": KIND_HTML,
        "html_page": KIND_HTML,
        "pdf": KIND_PDF,
        "docx": KIND_DOCX,
    }
    for kind in kinds:
        key = alias.get(kind.strip().lower())
        if key is None:
            raise ValueError(f"unsupported extraction kind: {kind!r}")
        if key not in normalized:
            normalized.append(key)
    return normalized


def _select_versions(
    session: Session,
    *,
    document_id: int | None,
    version_id: int | None,
    include_non_current: bool,
    content_kinds: list[str],
) -> list[tuple[Document, DocumentVersion]]:
    if version_id is not None:
        version = session.get(DocumentVersion, version_id)
        if version is None:
            return []
        document = session.get(Document, version.document_id)
        if document is None:
            return []
        if document_id is not None and document.id != document_id:
            return []
        return [(document, version)]

    stmt = (
        select(Document, DocumentVersion)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(Document.content_kind.in_(content_kinds))
        .order_by(Document.id.asc(), DocumentVersion.version_number.asc())
    )
    if document_id is not None:
        stmt = stmt.where(Document.id == document_id)
    if not include_non_current:
        stmt = stmt.where(DocumentVersion.is_current.is_(True))
    return list(session.execute(stmt).all())


def _commit_extraction_result(
    session: Session,
    result: ExtractionResult,
    *,
    storage_root: Path | str | None = None,
) -> ExtractionResult:
    root = data_root(storage_root)
    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001 — surface as failed extraction
        session.rollback()
        if result.storage_path:
            delete_quiet(root / result.storage_path)
        return ExtractionResult(
            document_id=result.document_id,
            document_version_id=result.document_version_id,
            version_number=result.version_number,
            status=STATUS_FAILED,
            content_kind=result.content_kind,
            extractor_name=result.extractor_name,
            extractor_version=result.extractor_version,
            title=result.title,
            content_sha256=result.content_sha256,
            block_count=result.block_count,
            message=f"commit failed after persist: {exc}",
        )
    return result


def extract_batch(
    session: Session,
    *,
    persist: bool = False,
    document_id: int | None = None,
    version_id: int | None = None,
    max_documents: int = 10,
    include_non_current: bool = False,
    storage_root: Path | str | None = None,
    kinds: list[str] | tuple[str, ...] | None = None,
    commit_each: bool = False,
) -> ExtractionBatchReport:
    """Extract structured content for a bounded set of stored versions.

    When ``persist`` and ``commit_each`` are both True, each version is
    committed before the next so a later failure cannot undo earlier writes.
    """
    if max_documents < 1:
        raise ValueError("max_documents must be >= 1")
    content_kinds = _normalize_kinds(kinds)

    pairs = _select_versions(
        session,
        document_id=document_id,
        version_id=version_id,
        include_non_current=include_non_current,
        content_kinds=content_kinds,
    )
    report = ExtractionBatchReport()
    if not pairs:
        report.message = (
            "no matching document versions for kinds="
            + ",".join(content_kinds)
        )
        return report

    def _run(document: Document, version: DocumentVersion) -> ExtractionResult:
        result = extract_document_version(
            session,
            version,
            document,
            persist=persist,
            storage_root=storage_root,
        )
        if persist and commit_each:
            return _commit_extraction_result(
                session, result, storage_root=storage_root
            )
        return result

    if version_id is not None:
        document, version = pairs[0]
        report.results.append(_run(document, version))
        return report

    allowed_docs: set[int] = set()
    for document, _version in pairs:
        if document.id in allowed_docs:
            continue
        if len(allowed_docs) >= max_documents:
            break
        allowed_docs.add(document.id)

    for document, version in pairs:
        if document.id not in allowed_docs:
            continue
        report.results.append(_run(document, version))
    return report


def extract_html_batch(
    session: Session,
    *,
    persist: bool = False,
    document_id: int | None = None,
    version_id: int | None = None,
    max_documents: int = 10,
    include_non_current: bool = False,
    storage_root: Path | str | None = None,
    commit_each: bool = False,
) -> ExtractionBatchReport:
    """Back-compat HTML-only batch extraction."""
    return extract_batch(
        session,
        persist=persist,
        document_id=document_id,
        version_id=version_id,
        max_documents=max_documents,
        include_non_current=include_non_current,
        storage_root=storage_root,
        commit_each=commit_each,
        kinds=[KIND_HTML],
    )
