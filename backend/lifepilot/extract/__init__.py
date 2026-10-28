"""Structured extraction from stored document versions (no crawling)."""

from lifepilot.extract.docx_structured import (
    EXTRACTOR_NAME as DOCX_EXTRACTOR_NAME,
)
from lifepilot.extract.docx_structured import (
    EXTRACTOR_VERSION as DOCX_EXTRACTOR_VERSION,
)
from lifepilot.extract.docx_structured import extract_docx_document
from lifepilot.extract.html_structured import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    StructuredDocument,
    extract_html_document,
    normalized_content_sha256,
)
from lifepilot.extract.pdf_structured import (
    EXTRACTOR_NAME as PDF_EXTRACTOR_NAME,
)
from lifepilot.extract.pdf_structured import (
    EXTRACTOR_VERSION as PDF_EXTRACTOR_VERSION,
)
from lifepilot.extract.pdf_structured import extract_pdf_document
from lifepilot.extract.runner import (
    STATUS_DRY_RUN,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    STATUS_UNCHANGED,
    ExtractionBatchReport,
    ExtractionResult,
    extract_batch,
    extract_document_version,
    extract_html_batch,
    extract_html_version,
)

__all__ = [
    "DOCX_EXTRACTOR_NAME",
    "DOCX_EXTRACTOR_VERSION",
    "EXTRACTOR_NAME",
    "EXTRACTOR_VERSION",
    "ExtractionBatchReport",
    "ExtractionResult",
    "PDF_EXTRACTOR_NAME",
    "PDF_EXTRACTOR_VERSION",
    "STATUS_DRY_RUN",
    "STATUS_EMPTY",
    "STATUS_FAILED",
    "STATUS_SKIPPED",
    "STATUS_SUCCESS",
    "STATUS_UNCHANGED",
    "StructuredDocument",
    "extract_batch",
    "extract_document_version",
    "extract_docx_document",
    "extract_html_batch",
    "extract_html_document",
    "extract_html_version",
    "extract_pdf_document",
    "normalized_content_sha256",
]
