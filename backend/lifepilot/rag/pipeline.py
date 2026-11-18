"""Populate ``document_chunks`` from successful current extractions."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, joinedload

from lifepilot.crawl.storage import data_root, resolve_under_data_root
from lifepilot.db.models import (
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentVersion,
    Jurisdiction,
    Source,
)
from lifepilot.rag.chunker import ChunkDraft, chunk_structured_document

logger = logging.getLogger(__name__)

EXTRACTOR_PRIORITY = (
    "html_structured",
    "pdf_structured",
    "docx_structured",
)


@dataclass
class ChunkRunStats:
    eligible_versions: int = 0
    processed: int = 0
    skipped_unchanged: int = 0
    skipped_duplicate_historical: int = 0
    skipped_no_extraction: int = 0
    skipped_missing_file: int = 0
    skipped_empty_chunks: int = 0
    skipped_no_jurisdiction: int = 0
    failed: int = 0
    chunks_written: int = 0
    chunks_deleted: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    by_jurisdiction: dict[str, int] = field(default_factory=dict)
    chunk_lengths: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        lengths = sorted(self.chunk_lengths)

        def pct(p: float) -> int:
            if not lengths:
                return 0
            idx = int(round((len(lengths) - 1) * p))
            return lengths[idx]

        return {
            "eligible_versions": self.eligible_versions,
            "processed": self.processed,
            "skipped_unchanged": self.skipped_unchanged,
            "skipped_duplicate_historical": self.skipped_duplicate_historical,
            "skipped_no_extraction": self.skipped_no_extraction,
            "skipped_missing_file": self.skipped_missing_file,
            "skipped_empty_chunks": self.skipped_empty_chunks,
            "skipped_no_jurisdiction": self.skipped_no_jurisdiction,
            "failed": self.failed,
            "chunks_written": self.chunks_written,
            "chunks_deleted": self.chunks_deleted,
            "by_jurisdiction": dict(sorted(self.by_jurisdiction.items())),
            "chunk_length": {
                "count": len(lengths),
                "min": lengths[0] if lengths else 0,
                "p50": pct(0.5),
                "p90": pct(0.9),
                "max": lengths[-1] if lengths else 0,
                "mean": int(sum(lengths) / len(lengths)) if lengths else 0,
            },
            "failures": self.failures[:50],
        }


def _pick_extraction(
    extractions: list[DocumentExtraction],
) -> DocumentExtraction | None:
    success = [e for e in extractions if e.status == "success" and e.storage_path]
    if not success:
        return None
    rank = {name: i for i, name in enumerate(EXTRACTOR_PRIORITY)}
    success.sort(key=lambda e: rank.get(e.extractor_name, 99))
    return success[0]


def _load_payload(root: Path, storage_path: str) -> dict[str, Any]:
    path = resolve_under_data_root(root, storage_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"extraction JSON is not an object: {storage_path}")
    return data


def _fingerprint(drafts: list[ChunkDraft]) -> tuple[tuple[int, str], ...]:
    return tuple((d.chunk_index, d.content_sha256) for d in drafts)


def _existing_fingerprint(
    session: Session, version_id: int
) -> tuple[tuple[int, str], ...]:
    rows = session.execute(
        select(DocumentChunk.chunk_index, DocumentChunk.content_sha256)
        .where(DocumentChunk.document_version_id == version_id)
        .order_by(DocumentChunk.chunk_index)
    ).all()
    return tuple((int(r[0]), str(r[1])) for r in rows)


def iter_eligible_versions(
    session: Session,
    *,
    document_ids: list[int] | None = None,
    version_ids: list[int] | None = None,
    jurisdiction_codes: list[str] | None = None,
    limit: int | None = None,
    include_historical: bool = False,
) -> list[DocumentVersion]:
    """Eligible versions ordered by id (current only unless ``include_historical=True``)."""
    stmt = (
        select(DocumentVersion)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.primary_source_id)
        .join(Jurisdiction, Jurisdiction.id == Source.jurisdiction_id)
        .options(
            joinedload(DocumentVersion.document)
            .joinedload(Document.primary_source)
            .joinedload(Source.jurisdiction),
            joinedload(DocumentVersion.extractions),
        )
        .order_by(
            DocumentVersion.document_id,
            DocumentVersion.is_current.desc(),
            DocumentVersion.version_number.asc(),
            DocumentVersion.id,
        )
    )
    if not include_historical:
        stmt = stmt.where(DocumentVersion.is_current.is_(True))
    if document_ids:
        stmt = stmt.where(DocumentVersion.document_id.in_(document_ids))
    if version_ids:
        stmt = stmt.where(DocumentVersion.id.in_(version_ids))
    if jurisdiction_codes:
        codes = [c.upper() for c in jurisdiction_codes]
        stmt = stmt.where(Jurisdiction.code.in_(codes))
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).unique().all())


def _has_equivalent_chunked_version(
    session: Session,
    *,
    document_id: int,
    version_id: int,
    extraction_sha256: str | None,
    raw_sha256: str,
) -> bool:
    """True when another historical chunked version of the same doc has identical content."""
    other_versions = session.execute(
        select(
            DocumentVersion.id,
            DocumentVersion.content_sha256,
        )
        .where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.id != version_id,
            DocumentVersion.is_current.is_(False),
            DocumentVersion.id.in_(
                select(DocumentChunk.document_version_id).distinct()
            ),
        )
    ).all()
    if not other_versions:
        return False

    other_ids = [int(r[0]) for r in other_versions]
    if extraction_sha256:
        ext_match = session.scalar(
            select(DocumentExtraction.id)
            .where(
                DocumentExtraction.document_version_id.in_(other_ids),
                DocumentExtraction.status == "success",
                DocumentExtraction.content_sha256 == extraction_sha256,
            )
            .limit(1)
        )
        if ext_match is not None:
            return True

    return any(str(r[1]) == raw_sha256 for r in other_versions)


def sync_chunk_currency_flags(
    session: Session,
    *,
    document_id: int,
) -> int:
    """Keep ``document_chunks.is_current_version`` aligned with ``document_versions.is_current``."""
    from sqlalchemy import update

    current_ids = list(
        session.scalars(
            select(DocumentVersion.id).where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.is_current.is_(True),
            )
        ).all()
    )
    updated = 0
    if current_ids:
        res1 = session.execute(
            update(DocumentChunk)
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.document_version_id.in_(current_ids),
                DocumentChunk.is_current_version.is_(False),
            )
            .values(is_current_version=True)
        )
        updated += int(res1.rowcount or 0)
        res2 = session.execute(
            update(DocumentChunk)
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.document_version_id.not_in(current_ids),
                DocumentChunk.is_current_version.is_(True),
            )
            .values(is_current_version=False)
        )
        updated += int(res2.rowcount or 0)
    else:
        res = session.execute(
            update(DocumentChunk)
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.is_current_version.is_(True),
            )
            .values(is_current_version=False)
        )
        updated += int(res.rowcount or 0)
    return updated


def chunk_one_version(
    session: Session,
    version: DocumentVersion,
    *,
    data_dir: Path,
    force: bool = False,
    allow_non_current: bool = False,
    dedupe_historical: bool = True,
) -> tuple[str, int, int]:
    """Chunk one version (current-only unless ``allow_non_current=True``).

    Returns ``(status, chunks_written, chunks_deleted)``.
    """
    if not version.is_current and not allow_non_current:
        return "skipped_not_current", 0, 0

    document = version.document
    source = document.primary_source
    jurisdiction = source.jurisdiction
    if jurisdiction is None or not (jurisdiction.code or "").strip():
        return "skipped_no_jurisdiction", 0, 0

    extraction = _pick_extraction(list(version.extractions or []))
    if extraction is None:
        return "skipped_no_extraction", 0, 0

    if (
        not version.is_current
        and allow_non_current
        and dedupe_historical
        and not force
        and _has_equivalent_chunked_version(
            session,
            document_id=document.id,
            version_id=version.id,
            extraction_sha256=extraction.content_sha256,
            raw_sha256=version.content_sha256,
        )
    ):
        return "skipped_duplicate_historical", 0, 0

    try:
        payload = _load_payload(data_dir, extraction.storage_path or "")
    except FileNotFoundError:
        return "skipped_missing_file", 0, 0

    drafts = chunk_structured_document(payload)
    if not drafts:
        return "skipped_empty_chunks", 0, 0

    fp = _fingerprint(drafts)
    if not force and _existing_fingerprint(session, version.id) == fp:
        sync_chunk_currency_flags(session, document_id=document.id)
        return "skipped_unchanged", 0, 0

    deleted = session.execute(
        delete(DocumentChunk).where(DocumentChunk.document_version_id == version.id)
    )
    deleted_n = int(deleted.rowcount or 0)

    is_curr = bool(version.is_current)
    for draft in drafts:
        session.add(
            DocumentChunk(
                document_id=document.id,
                document_version_id=version.id,
                source_id=source.id,
                jurisdiction_id=jurisdiction.id,
                jurisdiction_code=jurisdiction.code,
                pathway_id=None,
                requirement_category=None,
                heading_path=draft.heading_path,
                char_start=draft.char_start,
                char_end=draft.char_end,
                chunk_index=draft.chunk_index,
                content_text=draft.content_text,
                content_sha256=draft.content_sha256,
                is_current_version=is_curr,
            )
        )
    session.flush()
    sync_chunk_currency_flags(session, document_id=document.id)
    return "processed", len(drafts), deleted_n


def run_chunking(
    session: Session,
    *,
    data_dir: Path | str | None = None,
    document_ids: list[int] | None = None,
    version_ids: list[int] | None = None,
    jurisdiction_codes: list[str] | None = None,
    limit: int | None = None,
    batch_size: int = 25,
    force: bool = False,
    include_historical: bool = False,
    dedupe_historical: bool = True,
    progress: Callable[[str], None] | None = None,
) -> ChunkRunStats:
    """Chunk eligible extractions into ``document_chunks``.

    Defaults to ``include_historical=False`` (current versions only). Passing
    ``include_historical=True`` enables bounded, deduplicated indexing of
    historical versions with ``is_current_version=False``.
    """
    root = data_root(data_dir)
    stats = ChunkRunStats()
    versions = iter_eligible_versions(
        session,
        document_ids=document_ids,
        version_ids=version_ids,
        jurisdiction_codes=jurisdiction_codes,
        limit=limit,
        include_historical=include_historical,
    )
    stats.eligible_versions = len(versions)
    if progress:
        progress(f"Eligible versions: {stats.eligible_versions}")

    since_commit = 0
    for i, version in enumerate(versions, start=1):
        try:
            status, written, deleted_n = chunk_one_version(
                session,
                version,
                data_dir=root,
                force=force,
                allow_non_current=include_historical,
                dedupe_historical=dedupe_historical,
            )
            if status == "processed":
                stats.processed += 1
                stats.chunks_written += written
                stats.chunks_deleted += deleted_n
                code = version.document.primary_source.jurisdiction.code
                stats.by_jurisdiction[code] = (
                    stats.by_jurisdiction.get(code, 0) + written
                )
                stats.chunk_lengths.extend(
                    len(t)
                    for t in session.execute(
                        select(DocumentChunk.content_text).where(
                            DocumentChunk.document_version_id == version.id
                        )
                    )
                    .scalars()
                    .all()
                )
            elif status == "skipped_unchanged":
                stats.skipped_unchanged += 1
            elif status == "skipped_duplicate_historical":
                stats.skipped_duplicate_historical += 1
            elif status == "skipped_no_extraction":
                stats.skipped_no_extraction += 1
            elif status == "skipped_missing_file":
                stats.skipped_missing_file += 1
            elif status == "skipped_empty_chunks":
                stats.skipped_empty_chunks += 1
            elif status == "skipped_no_jurisdiction":
                stats.skipped_no_jurisdiction += 1

            since_commit += 1
            if since_commit >= batch_size:
                session.commit()
                since_commit = 0
                if progress:
                    progress(
                        f"Progress {i}/{stats.eligible_versions} "
                        f"(processed={stats.processed}, "
                        f"unchanged={stats.skipped_unchanged}, "
                        f"failed={stats.failed})"
                    )
        except Exception as exc:  # noqa: BLE001 — isolate per-document failures
            session.rollback()
            since_commit = 0
            stats.failed += 1
            stats.failures.append(
                {
                    "document_id": version.document_id,
                    "document_version_id": version.id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            logger.exception(
                "chunking failed document_id=%s version_id=%s",
                version.document_id,
                version.id,
            )

    if since_commit:
        session.commit()
    return stats
