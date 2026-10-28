"""Shared structured-extraction schema (HTML / PDF / DOCX)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 2

_WS_RE = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return _WS_RE.sub(" ", value).strip()


@dataclass(frozen=True)
class ContentBlock:
    type: str
    text: str | None = None
    level: int | None = None
    ordered: bool | None = None
    items: tuple[str, ...] | None = None
    headers: tuple[str, ...] | None = None
    rows: tuple[tuple[str, ...], ...] | None = None
    caption: str | None = None
    page: int | None = None  # 1-based PDF page; None for HTML/DOCX

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type}
        if self.type == "heading":
            data["level"] = self.level
            data["text"] = self.text or ""
        elif self.type == "paragraph":
            data["text"] = self.text or ""
        elif self.type == "list":
            data["ordered"] = bool(self.ordered)
            data["items"] = list(self.items or ())
        elif self.type == "table":
            if self.caption:
                data["caption"] = self.caption
            data["headers"] = list(self.headers or ())
            data["rows"] = [list(row) for row in (self.rows or ())]
        else:
            if self.text is not None:
                data["text"] = self.text
        if self.page is not None:
            data["page"] = self.page
        return data


@dataclass
class StructuredDocument:
    title: str
    blocks: list[ContentBlock] = field(default_factory=list)
    page_count: int | None = None
    empty_pages: tuple[int, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_content_dict(self) -> dict[str, Any]:
        """Canonical content used for SHA-256 (no run/extractor metadata)."""
        payload: dict[str, Any] = {
            "title": self.title,
            "blocks": [block.to_dict() for block in self.blocks],
        }
        if self.page_count is not None:
            payload["page_count"] = self.page_count
        if self.empty_pages:
            payload["empty_pages"] = list(self.empty_pages)
        # Only stable, content-relevant metadata keys.
        meta = {
            key: value
            for key, value in sorted(self.metadata.items())
            if key in {"author", "subject", "keywords", "created", "modified"}
            and value not in (None, "")
        }
        if meta:
            payload["metadata"] = meta
        return payload

    def to_storage_dict(
        self,
        *,
        extractor: str,
        extractor_version: str,
        document_version_id: int | None = None,
        document_id: int | None = None,
        source_url: str | None = None,
        content_sha256: str | None = None,
        schema_version: int = SCHEMA_VERSION,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": schema_version,
            "extractor": extractor,
            "extractor_version": extractor_version,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "source_url": source_url,
            "content_sha256": content_sha256,
            **self.to_content_dict(),
        }
        return payload


def normalized_content_sha256(document: StructuredDocument) -> str:
    """SHA-256 of canonical JSON for extractable content (ignores run metadata)."""
    canonical = json.dumps(
        document.to_content_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
