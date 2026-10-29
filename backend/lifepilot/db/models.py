"""Government knowledge collection schema.

Applicability of a document to jurisdictions is explicit via
``document_jurisdictions`` only. Do not infer country/region coverage from
the publishing authority or jurisdiction hierarchy alone.

``primary_source_id`` and ``fetched_via_source_id`` record first ownership and
per-fetch crawl path. They do not provide complete multi-source discovery
provenance; a future ``document_source_sightings`` table will cover that.

Raw content-change detection for fetch dedupe compares a new SHA-256 only
against the row where ``is_current`` is true—not against all historical
versions. The same raw hash may reappear later as a new version. Meaningful
guidance changes are detected via ``document_content_comparisons`` over
successful structured extractions, not raw bytes alone.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CHAR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lifepilot.db.base import Base

JURISDICTION_TYPES = (
    "supranational",
    "country",
    "state",
    "province",
    "region",
    "municipality",
    "other",
)
SOURCE_REVIEW_STATUSES = ("approved", "candidate", "rejected")
DOCUMENT_CONTENT_KINDS = ("html_page", "pdf", "docx", "other")
DOCUMENT_DISCOVERY_STATUSES = ("seed", "discovered", "approved", "rejected")
DOCUMENT_JURISDICTION_RELEVANCE = ("applicable", "related")
DOCUMENT_EXTRACTION_STATUSES = ("success", "empty", "failed", "skipped")
DOCUMENT_COMPARISON_STATUSES = ("unchanged", "changed", "unavailable")


class Jurisdiction(Base):
    __tablename__ = "jurisdictions"
    __table_args__ = (
        CheckConstraint(
            "jurisdiction_type IN ("
            + ", ".join(f"'{t}'" for t in JURISDICTION_TYPES)
            + ")",
            name="ck_jurisdictions_type",
        ),
        Index("ix_jurisdictions_parent_id", "parent_id"),
        Index("ix_jurisdictions_country_code", "country_code"),
        Index("ix_jurisdictions_jurisdiction_type", "jurisdiction_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    parent_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("jurisdictions.id"), nullable=True
    )
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    jurisdiction_type: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[Optional[str]] = mapped_column(CHAR(2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    parent: Mapped[Optional[Jurisdiction]] = relationship(
        remote_side="Jurisdiction.id", back_populates="children"
    )
    children: Mapped[list[Jurisdiction]] = relationship(back_populates="parent")
    authorities: Mapped[list[Authority]] = relationship(back_populates="jurisdiction")
    sources: Mapped[list[Source]] = relationship(back_populates="jurisdiction")
    document_links: Mapped[list[DocumentJurisdiction]] = relationship(
        back_populates="jurisdiction"
    )


class Authority(Base):
    __tablename__ = "authorities"
    __table_args__ = (
        UniqueConstraint("jurisdiction_id", "name", name="uq_authorities_jurisdiction_name"),
        Index("ix_authorities_jurisdiction_id", "jurisdiction_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    jurisdiction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jurisdictions.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    short_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    website_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    jurisdiction: Mapped[Jurisdiction] = relationship(back_populates="authorities")
    sources: Mapped[list[Source]] = relationship(back_populates="authority")
    documents: Mapped[list[Document]] = relationship(back_populates="authority")


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(
            "review_status IN ("
            + ", ".join(f"'{s}'" for s in SOURCE_REVIEW_STATUSES)
            + ")",
            name="ck_sources_review_status",
        ),
        Index("ix_sources_authority_id", "authority_id"),
        Index("ix_sources_jurisdiction_id", "jurisdiction_id"),
        Index("ix_sources_review_status", "review_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    authority_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("authorities.id"), nullable=False
    )
    jurisdiction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jurisdictions.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    seed_url: Mapped[str] = mapped_column(Text, nullable=False)
    seed_url_normalized: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    allowed_url_patterns: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(Text), nullable=True
    )
    review_status: Mapped[str] = mapped_column(Text, nullable=False)
    default_language: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    authority: Mapped[Authority] = relationship(back_populates="sources")
    jurisdiction: Mapped[Jurisdiction] = relationship(back_populates="sources")
    primary_documents: Mapped[list[Document]] = relationship(
        back_populates="primary_source"
    )
    fetched_versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="fetched_via_source"
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "content_kind IN ("
            + ", ".join(f"'{k}'" for k in DOCUMENT_CONTENT_KINDS)
            + ")",
            name="ck_documents_content_kind",
        ),
        CheckConstraint(
            "discovery_status IN ("
            + ", ".join(f"'{s}'" for s in DOCUMENT_DISCOVERY_STATUSES)
            + ")",
            name="ck_documents_discovery_status",
        ),
        Index("ix_documents_primary_source_id", "primary_source_id"),
        Index("ix_documents_authority_id", "authority_id"),
        Index("ix_documents_translation_group_id", "translation_group_id"),
        Index("ix_documents_discovery_status", "discovery_status"),
        Index("ix_documents_content_kind", "content_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    authority_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("authorities.id"), nullable=True
    )
    primary_source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id"), nullable=False
    )
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    url_normalized: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    content_kind: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    translation_group_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    discovery_status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    authority: Mapped[Optional[Authority]] = relationship(back_populates="documents")
    primary_source: Mapped[Source] = relationship(back_populates="primary_documents")
    versions: Mapped[list[DocumentVersion]] = relationship(back_populates="document")
    jurisdiction_links: Mapped[list[DocumentJurisdiction]] = relationship(
        back_populates="document"
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "version_number", name="uq_document_versions_document_version"
        ),
        # At most one current version per document (PostgreSQL partial unique index).
        Index(
            "uq_document_versions_one_current",
            "document_id",
            unique=True,
            postgresql_where=text("is_current IS TRUE"),
        ),
        Index("ix_document_versions_document_retrieved", "document_id", "retrieved_at"),
        Index("ix_document_versions_content_sha256", "content_sha256"),
        Index("ix_document_versions_fetched_via_source_id", "fetched_via_source_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id"), nullable=False
    )
    fetched_via_source_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("sources.id"), nullable=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    language: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    byte_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="versions")
    fetched_via_source: Mapped[Optional[Source]] = relationship(
        back_populates="fetched_versions"
    )
    extractions: Mapped[list["DocumentExtraction"]] = relationship(
        back_populates="document_version"
    )


class DocumentExtraction(Base):
    """Structured extraction derived from an immutable document version.

    Raw ``document_versions`` rows and files are never overwritten. Re-running
    the same extractor updates this row and the derived JSON only.
    """

    __tablename__ = "document_extractions"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            + ", ".join(f"'{s}'" for s in DOCUMENT_EXTRACTION_STATUSES)
            + ")",
            name="ck_document_extractions_status",
        ),
        UniqueConstraint(
            "document_version_id",
            "extractor_name",
            name="uq_document_extractions_version_extractor",
        ),
        Index("ix_document_extractions_document_version_id", "document_version_id"),
        Index("ix_document_extractions_status", "status"),
        Index("ix_document_extractions_content_sha256", "content_sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("document_versions.id"), nullable=False
    )
    extractor_name: Mapped[str] = mapped_column(Text, nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[Optional[str]] = mapped_column(CHAR(64), nullable=True)
    storage_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    document_version: Mapped[DocumentVersion] = relationship(
        back_populates="extractions"
    )


class DocumentContentComparison(Base):
    """Meaningful content comparison between two document versions.

    Compares successful structured extractions only. Historical rows are not
    silently overwritten when re-run with different hashes or status.
    """

    __tablename__ = "document_content_comparisons"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            + ", ".join(f"'{s}'" for s in DOCUMENT_COMPARISON_STATUSES)
            + ")",
            name="ck_document_content_comparisons_status",
        ),
        UniqueConstraint(
            "previous_version_id",
            "current_version_id",
            "extractor_name",
            "extractor_version",
            "algorithm_version",
            name="uq_document_content_comparisons_pair",
        ),
        Index("ix_document_content_comparisons_document_id", "document_id"),
        Index("ix_document_content_comparisons_status", "status"),
        Index("ix_document_content_comparisons_compared_at", "compared_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id"), nullable=False
    )
    previous_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("document_versions.id"), nullable=False
    )
    current_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("document_versions.id"), nullable=False
    )
    extractor_name: Mapped[str] = mapped_column(Text, nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    previous_content_sha256: Mapped[Optional[str]] = mapped_column(
        CHAR(64), nullable=True
    )
    current_content_sha256: Mapped[Optional[str]] = mapped_column(
        CHAR(64), nullable=True
    )
    storage_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    unavailable_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    compared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DocumentJurisdiction(Base):
    """Explicit document↔jurisdiction applicability (never inferred automatically)."""

    __tablename__ = "document_jurisdictions"
    __table_args__ = (
        CheckConstraint(
            "relevance IN ("
            + ", ".join(f"'{r}'" for r in DOCUMENT_JURISDICTION_RELEVANCE)
            + ")",
            name="ck_document_jurisdictions_relevance",
        ),
        Index("ix_document_jurisdictions_jurisdiction_id", "jurisdiction_id"),
    )

    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    jurisdiction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jurisdictions.id"), primary_key=True
    )
    relevance: Mapped[str] = mapped_column(Text, nullable=False, default="applicable")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="jurisdiction_links")
    jurisdiction: Mapped[Jurisdiction] = relationship(back_populates="document_links")
