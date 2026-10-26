"""Validate downloaded PDF/DOCX bytes (magic + structure, not extension alone)."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass


@dataclass(frozen=True)
class BinaryValidationResult:
    ok: bool
    content_kind: str | None  # pdf | docx
    reason: str = ""


def _looks_like_html(body: bytes) -> bool:
    sample = body.lstrip()[:256].lower()
    return sample.startswith(
        (b"<!doctype html", b"<html", b"<head", b"<body", b"<!--")
    )


def validate_pdf_bytes(body: bytes) -> BinaryValidationResult:
    if not body:
        return BinaryValidationResult(False, None, "empty_body")
    if _looks_like_html(body):
        return BinaryValidationResult(False, None, "html_disguised_as_pdf")
    if not body.lstrip().startswith(b"%PDF"):
        return BinaryValidationResult(False, None, "missing_pdf_magic")
    return BinaryValidationResult(True, "pdf", "pdf_magic_ok")


def validate_docx_bytes(
    body: bytes,
    *,
    max_uncompressed_bytes: int,
) -> BinaryValidationResult:
    if not body:
        return BinaryValidationResult(False, None, "empty_body")
    if _looks_like_html(body):
        return BinaryValidationResult(False, None, "html_disguised_as_docx")
    if not body.startswith(b"PK\x03\x04") and not body.startswith(b"PK\x05\x06"):
        return BinaryValidationResult(False, None, "missing_zip_magic")

    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = set(zf.namelist())
            # Required OOXML parts (forward-slash paths inside ZIP).
            if "[Content_Types].xml" not in names:
                return BinaryValidationResult(
                    False, None, "docx_missing_content_types"
                )
            if "word/document.xml" not in names:
                return BinaryValidationResult(
                    False, None, "docx_missing_word_document"
                )

            total_uncompressed = 0
            for info in zf.infolist():
                if info.file_size < 0 or info.compress_size < 0:
                    return BinaryValidationResult(False, None, "docx_invalid_sizes")
                total_uncompressed += int(info.file_size)
                if total_uncompressed > max_uncompressed_bytes:
                    return BinaryValidationResult(
                        False, None, "docx_uncompressed_too_large"
                    )
                # Extreme compression ratio → likely zip bomb.
                if info.compress_size > 0 and info.file_size // info.compress_size > 100:
                    return BinaryValidationResult(
                        False, None, "docx_suspicious_compression_ratio"
                    )
                # Do not extract members to disk; reading one small required
                # file confirms the archive is readable without unpacking all.
            # Spot-check that Content_Types is readable XML-ish.
            raw = zf.read("[Content_Types].xml")
            if b"Types" not in raw[:200] and b"Override" not in raw[:400]:
                return BinaryValidationResult(
                    False, None, "docx_content_types_not_ooxml"
                )
    except zipfile.BadZipFile:
        return BinaryValidationResult(False, None, "docx_bad_zip")
    except (OSError, RuntimeError, ValueError) as exc:
        return BinaryValidationResult(False, None, f"docx_zip_error:{type(exc).__name__}")

    return BinaryValidationResult(True, "docx", "docx_ooxml_ok")


def validate_binary_bytes(
    body: bytes,
    *,
    expected_kind: str,
    max_uncompressed_bytes: int,
) -> BinaryValidationResult:
    """Validate bytes for an expected document kind (pdf|docx)."""
    if expected_kind == "pdf":
        return validate_pdf_bytes(body)
    if expected_kind == "docx":
        return validate_docx_bytes(
            body, max_uncompressed_bytes=max_uncompressed_bytes
        )
    return BinaryValidationResult(False, None, f"unsupported_kind:{expected_kind}")
