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
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, CHAR, JSONB, UUID
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
FRONTIER_DISCOVERY_METHODS = (
    "seed",
    "html_link",
    "sitemap",
    "pageschema_link",
    "allowlist_exact",
)
FRONTIER_STATUSES = (
    "pending",
    "fetched",
    "skipped",
    "blocked",
    "failed",
    "out_of_scope",
)
FRONTIER_FAILURE_CLASSES = ("transient", "permanent")

# Planned default embedding model for Phase 8C (384-d). Future models with a
# different dimension require a new embeddings table/column — see EmbeddingModel.
PLANNED_EMBEDDING_MODEL_ID = "intfloat/multilingual-e5-small"
PLANNED_EMBEDDING_DIMENSIONS = 384
RAG_REQUIREMENT_CATEGORIES = (
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "official_provenance",
)

# Controlled legal-status classifications for retrieved document versions.
# Never treat HTTP headers, retrieved_at, or is_current as legal effective dates.
DOCUMENT_LEGAL_STATUSES = (
    "unknown",
    "in_force",
    "historical",
    "superseded",
    "future_pending",
    "proposed",
    "withdrawn",
    "archived",
    "transitional",
)

LEGAL_RULE_REQUIREMENT_CATEGORIES = (
    "eligibility",
    "required_documents",
    "application_process",
    "financial_or_sponsorship",
    "fees_costs",
    "conditions_duration",
    "language_requirements",
    "health_character",
    "dependents",
    "work_rights",
    "processing_times",
    "official_provenance",
)

LEGAL_RULE_TYPES = (
    "salary_threshold",
    "maintenance_funds",
    "application_fee",
    "surcharge",
    "income_threshold",
    "language_level",
    "work_hours_limit",
    "duration_limit",
    "residence_period",
    "age_limit",
    "eligibility_condition",
    "exemption",
    "transitional_rule",
    "document_requirement",
    "process_step",
    "other",
)

LEGAL_RULE_UNITS = (
    "yearly",
    "monthly",
    "weekly",
    "daily",
    "hourly",
    "one_time",
    "hours_per_week",
    "days",
    "months",
    "years",
    "percent",
    "points",
    "count",
    "other",
)

LEGAL_RULE_COMPARATORS = ("gte", "gt", "lte", "lt", "eq")

LEGAL_RULE_EFFECTIVE_DATE_STATUSES = (
    "unknown",
    "partially_known",
    "known_bounded",
    "known_open_ended",
    "announced_future",
    "superseded",
    "expired",
)

LEGAL_RULE_COHORT_CATEGORIES = (
    "general",
    "transitional",
    "age_based",
    "qualification_based",
    "new_entrant",
    "occupation_or_sector",
    "location_based",
    "sponsorship_or_route_history",
    "exemption",
    "other",
)

LEGAL_RULE_REVIEW_STATUSES = (
    "unreviewed",
    "pending_review",
    "approved",
    "rejected",
    "clarification_requested",
    "stale_pending_review",
)

LEGAL_RULE_VERIFICATION_STATUSES = (
    "unverified",
    "machine_extracted_unverified",
    "human_verified",
    "disputed",
    "potentially_outdated",
)

# ---------------------------------------------------------------------------
# Phase 10D.1 — Persistent legal review governance
# ---------------------------------------------------------------------------

LEGAL_REVIEW_SUBJECT_TYPES = (
    "legal_rule",
    "claim_evidence",
    "high_risk_queue_item",
)

LEGAL_REVIEW_WORKFLOW_STATES = (
    "pending",
    "assigned",
    "in_review",
    "approved",
    "rejected",
    "changes_requested",
    "reopened",
    "invalidated",
    "superseded",
)

LEGAL_REVIEW_EVENT_TYPES = (
    "submitted_for_review",
    "assigned",
    "review_started",
    "approved",
    "rejected",
    "changes_requested",
    "reopened",
    "approval_invalidated",
    "source_version_changed",
    "superseded",
    "administrative_correction",
)

LEGAL_REVIEW_ACTOR_KINDS = (
    "human_reviewer",
    "service",
    "system",
)

LEGAL_REVIEW_RISK_CLASSES = (
    "critical",
    "high",
    "medium",
    "low",
)

LEGAL_REVIEW_PRIORITIES = (
    "p0",
    "p1",
    "p2",
    "p3",
)


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
        CheckConstraint(
            "legal_status IN ("
            + ", ".join(f"'{s}'" for s in DOCUMENT_LEGAL_STATUSES)
            + ")",
            name="ck_document_versions_legal_status",
        ),
        CheckConstraint(
            "superseded_by_version_id IS NULL OR superseded_by_version_id <> id",
            name="ck_document_versions_not_self_superseded",
        ),
        CheckConstraint(
            "http_etag IS NULL OR length(trim(http_etag)) > 0",
            name="ck_document_versions_http_etag_nonempty",
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
        Index("ix_document_versions_legal_status", "legal_status"),
        Index("ix_document_versions_superseded_by_version_id", "superseded_by_version_id"),
        Index("ix_document_versions_source_updated_at", "source_updated_at"),
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
    # Successful unchanged recheck time; never replaces retrieved_at provenance.
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Observed source / HTTP timestamps (never treated as legal effective dates).
    http_last_modified: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    http_etag: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Source-level legal classification (distinct from crawl currency is_current).
    legal_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="unknown", server_default=text("'unknown'")
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    superseded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    superseded_by_version_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("document_versions.id"), nullable=True
    )
    temporal_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
    superseded_by_version: Mapped[Optional["DocumentVersion"]] = relationship(
        remote_side="DocumentVersion.id",
        foreign_keys=[superseded_by_version_id],
    )
    legal_rules: Mapped[list["LegalRule"]] = relationship(
        back_populates="document_version",
        foreign_keys="LegalRule.document_version_id",
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
            "result_revision",
            name="uq_document_content_comparisons_pair_revision",
        ),
        Index("ix_document_content_comparisons_document_id", "document_id"),
        Index("ix_document_content_comparisons_status", "status"),
        Index("ix_document_content_comparisons_compared_at", "compared_at"),
        Index("ix_document_content_comparisons_supersedes_id", "supersedes_id"),
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
    result_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", default=1
    )
    supersedes_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("document_content_comparisons.id"),
        nullable=True,
    )
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


class DiscoveryFrontier(Base):
    """Per-source URL discovery queue for resumable BFS crawling.

    Rows are enqueued before fetch. Successful fetches link ``document_id``.
    Transient failures remain ``pending`` with ``next_attempt_at`` set so they
    are not lost across runs.
    """

    __tablename__ = "discovery_frontier"
    __table_args__ = (
        CheckConstraint(
            "discovery_method IN ("
            + ", ".join(f"'{m}'" for m in FRONTIER_DISCOVERY_METHODS)
            + ")",
            name="ck_discovery_frontier_method",
        ),
        CheckConstraint(
            "status IN ("
            + ", ".join(f"'{s}'" for s in FRONTIER_STATUSES)
            + ")",
            name="ck_discovery_frontier_status",
        ),
        CheckConstraint(
            "failure_class IS NULL OR failure_class IN ("
            + ", ".join(f"'{c}'" for c in FRONTIER_FAILURE_CLASSES)
            + ")",
            name="ck_discovery_frontier_failure_class",
        ),
        CheckConstraint("depth >= 0", name="ck_discovery_frontier_depth"),
        CheckConstraint(
            "attempt_count >= 0", name="ck_discovery_frontier_attempt_count"
        ),
        UniqueConstraint(
            "source_id",
            "url_normalized",
            name="uq_discovery_frontier_source_url",
        ),
        Index(
            "ix_discovery_frontier_claim",
            "source_id",
            "status",
            "depth",
            "id",
        ),
        Index("ix_discovery_frontier_document_id", "document_id"),
        Index("ix_discovery_frontier_next_attempt_at", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id"), nullable=False
    )
    url_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    url_original: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parent_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    discovery_method: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    content_kind_hint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    last_attempted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_class: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    document_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("documents.id"), nullable=True
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    source: Mapped[Source] = relationship()
    document: Mapped[Optional[Document]] = relationship()


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


class EmbeddingModel(Base):
    """Registry of embedding models allowed for chunk vectors.

    The Phase 8B/8C embeddings table stores ``vector(384)`` for
    ``intfloat/multilingual-e5-small``. A future model with a different
    dimension must not reuse that column; add a new typed embeddings table
    (or column) and register the model here with matching ``dimensions``.
    """

    __tablename__ = "embedding_models"
    __table_args__ = (
        CheckConstraint("dimensions > 0", name="ck_embedding_models_dimensions"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    distance_metric: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'cosine'")
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    embeddings: Mapped[list["DocumentChunkEmbedding"]] = relationship(
        back_populates="embedding_model"
    )


class DocumentChunk(Base):
    """Evidence-aware text chunk prepared for future RAG indexing.

    Jurisdiction is mandatory and must come from authoritative source metadata
    (``sources.jurisdiction_id`` / ``jurisdictions.code``), not from empty
    ``document_jurisdictions`` rows or semantic similarity.

    ``pathway_id`` / ``requirement_category`` are optional curated associations
    only — never inferred from embeddings.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        CheckConstraint(
            "chunk_index >= 0",
            name="ck_document_chunks_chunk_index",
        ),
        CheckConstraint(
            "length(trim(content_text)) > 0",
            name="ck_document_chunks_content_nonempty",
        ),
        CheckConstraint(
            "char_start IS NULL OR char_start >= 0",
            name="ck_document_chunks_char_start",
        ),
        CheckConstraint(
            "char_end IS NULL OR char_start IS NULL OR char_end >= char_start",
            name="ck_document_chunks_char_range",
        ),
        CheckConstraint(
            "requirement_category IS NULL OR requirement_category IN ("
            + ", ".join(f"'{c}'" for c in RAG_REQUIREMENT_CATEGORIES)
            + ")",
            name="ck_document_chunks_requirement_category",
        ),
        CheckConstraint(
            "pathway_id IS NULL OR pathway_id ~ '^[a-z][a-z0-9_]*$'",
            name="ck_document_chunks_pathway_id",
        ),
        CheckConstraint(
            "length(trim(jurisdiction_code)) > 0",
            name="ck_document_chunks_jurisdiction_code_nonempty",
        ),
        UniqueConstraint(
            "document_version_id",
            "chunk_index",
            name="uq_document_chunks_version_index",
        ),
        # content_sha256 is indexed but not unique: identical passages may
        # legitimately appear more than once within one document version.
        Index("ix_document_chunks_document_id", "document_id"),
        Index("ix_document_chunks_document_version_id", "document_version_id"),
        Index("ix_document_chunks_source_id", "source_id"),
        Index("ix_document_chunks_jurisdiction_id", "jurisdiction_id"),
        Index(
            "ix_document_chunks_jurisdiction_current",
            "jurisdiction_code",
            "is_current_version",
        ),
        Index("ix_document_chunks_pathway_id", "pathway_id"),
        Index("ix_document_chunks_content_sha256", "content_sha256"),
        Index(
            "ix_document_chunks_retrievable",
            "jurisdiction_code",
            postgresql_where=text("is_current_version IS TRUE"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id"), nullable=False
    )
    document_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("document_versions.id"), nullable=False
    )
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id"), nullable=False
    )
    jurisdiction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jurisdictions.id"), nullable=False
    )
    # Denormalized from jurisdictions.code for mandatory filter speed.
    # Must match jurisdiction_id; enforced by trigger in migration.
    jurisdiction_code: Mapped[str] = mapped_column(Text, nullable=False)
    pathway_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requirement_category: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    heading_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    char_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    char_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # Snapshot of document_versions.is_current at index time; retrieval must
    # default to is_current_version IS TRUE (or join live version rows).
    # Full-text column search_tsv is DB-generated (see migration 20261107_0009).
    is_current_version: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    document: Mapped[Document] = relationship()
    document_version: Mapped[DocumentVersion] = relationship()
    source: Mapped[Source] = relationship()
    jurisdiction: Mapped[Jurisdiction] = relationship()
    embeddings: Mapped[list["DocumentChunkEmbedding"]] = relationship(
        back_populates="chunk", cascade="all, delete-orphan"
    )


class DocumentChunkEmbedding(Base):
    """384-dimensional embedding for a document chunk.

    Bound to ``embedding_models`` with ``dimensions = 384``. Changing to a
    non-384 model requires a new table/column — do not silently reuse this
    vector column for a different dimensionality.
    """

    __tablename__ = "document_chunk_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "chunk_id",
            "embedding_model_id",
            name="uq_document_chunk_embeddings_chunk_model",
        ),
        Index("ix_document_chunk_embeddings_model_id", "embedding_model_id"),
        Index("ix_document_chunk_embeddings_indexed_at", "indexed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chunk_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("document_chunks.id", ondelete="CASCADE"),
        nullable=False,
    )
    embedding_model_id: Mapped[str] = mapped_column(
        Text, ForeignKey("embedding_models.id"), nullable=False
    )
    embedding: Mapped[list[float]] = mapped_column(
        Vector(PLANNED_EMBEDDING_DIMENSIONS), nullable=False
    )
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    chunk: Mapped[DocumentChunk] = relationship(back_populates="embeddings")
    embedding_model: Mapped[EmbeddingModel] = relationship(back_populates="embeddings")


class LegalRule(Base):
    """Curated, temporally scoped immigration requirement or threshold rule.

    Each row represents one temporal/cohort revision of a rule identified by
    ``(rule_key, rule_revision)``. Multiple coexisting thresholds for different
    applicant cohorts (e.g., general vs. pre-cutoff transitional) are stored
    without colliding uniqueness constraints on pathway/category/date.

    Unknown effective dates (``effective_date_status='unknown'``) must not be
    treated as universally applicable. Referencing an official source/version
    never implies ``human_verified``; human verification requires explicit
    approval metadata and a pinned ``document_version_id``.
    """

    __tablename__ = "legal_rules"
    __table_args__ = (
        UniqueConstraint(
            "rule_key",
            "rule_revision",
            name="uq_legal_rules_key_revision",
        ),
        CheckConstraint(
            "rule_key ~ '^[a-z0-9][a-z0-9_.-]*$'",
            name="ck_legal_rules_rule_key",
        ),
        CheckConstraint(
            "rule_revision >= 1",
            name="ck_legal_rules_rule_revision",
        ),
        CheckConstraint(
            "supersedes_rule_id IS NULL OR supersedes_rule_id <> id",
            name="ck_legal_rules_not_self_superseded",
        ),
        CheckConstraint(
            "length(trim(jurisdiction_code)) > 0",
            name="ck_legal_rules_jurisdiction_code_nonempty",
        ),
        CheckConstraint(
            "pathway_id IS NULL OR pathway_id ~ '^[a-z][a-z0-9_]*$'",
            name="ck_legal_rules_pathway_id",
        ),
        CheckConstraint(
            "requirement_category IN ("
            + ", ".join(f"'{c}'" for c in LEGAL_RULE_REQUIREMENT_CATEGORIES)
            + ")",
            name="ck_legal_rules_requirement_category",
        ),
        CheckConstraint(
            "requirement_type IN ("
            + ", ".join(f"'{t}'" for t in LEGAL_RULE_TYPES)
            + ")",
            name="ck_legal_rules_requirement_type",
        ),
        CheckConstraint(
            "numeric_value IS NULL OR numeric_value >= 0",
            name="ck_legal_rules_numeric_nonnegative",
        ),
        CheckConstraint(
            "numeric_comparator IS NULL OR numeric_comparator IN ("
            + ", ".join(f"'{c}'" for c in LEGAL_RULE_COMPARATORS)
            + ")",
            name="ck_legal_rules_numeric_comparator",
        ),
        CheckConstraint(
            "numeric_max_value IS NULL OR (numeric_value IS NOT NULL AND numeric_max_value >= numeric_value)",
            name="ck_legal_rules_numeric_range",
        ),
        CheckConstraint(
            "currency_code IS NULL OR (currency_code ~ '^[A-Z]{3}$' AND numeric_value IS NOT NULL)",
            name="ck_legal_rules_currency_code",
        ),
        CheckConstraint(
            "amount_unit IS NULL OR (amount_unit IN ("
            + ", ".join(f"'{u}'" for u in LEGAL_RULE_UNITS)
            + ") AND numeric_value IS NOT NULL)",
            name="ck_legal_rules_amount_unit",
        ),
        CheckConstraint(
            "effective_date_status IN ("
            + ", ".join(f"'{s}'" for s in LEGAL_RULE_EFFECTIVE_DATE_STATUSES)
            + ")",
            name="ck_legal_rules_effective_date_status",
        ),
        CheckConstraint(
            "effective_date_status <> 'unknown' OR (effective_from IS NULL AND effective_to IS NULL)",
            name="ck_legal_rules_unknown_effective_dates",
        ),
        CheckConstraint(
            "effective_date_status NOT IN ('known_bounded', 'known_open_ended', 'announced_future') "
            "OR effective_from IS NOT NULL",
            name="ck_legal_rules_known_from_date",
        ),
        CheckConstraint(
            "effective_date_status <> 'known_bounded' OR effective_to IS NOT NULL",
            name="ck_legal_rules_known_to_date",
        ),
        CheckConstraint(
            "effective_from IS NULL OR effective_to IS NULL OR ("
            "(effective_from_inclusive AND effective_to_inclusive AND effective_to >= effective_from) "
            "OR ((NOT effective_from_inclusive OR NOT effective_to_inclusive) AND effective_to > effective_from)"
            ")",
            name="ck_legal_rules_effective_bounds_order",
        ),
        CheckConstraint(
            "cohort_key ~ '^[a-z0-9][a-z0-9_.-]*$'",
            name="ck_legal_rules_cohort_key",
        ),
        CheckConstraint(
            "cohort_category IN ("
            + ", ".join(f"'{c}'" for c in LEGAL_RULE_COHORT_CATEGORIES)
            + ")",
            name="ck_legal_rules_cohort_category",
        ),
        CheckConstraint(
            "jsonb_typeof(cohort_conditions) = 'object'",
            name="ck_legal_rules_cohort_conditions_object",
        ),
        CheckConstraint(
            "length(trim(rule_summary)) > 0",
            name="ck_legal_rules_rule_summary_nonempty",
        ),
        CheckConstraint(
            "document_version_id IS NOT NULL OR document_id IS NOT NULL OR source_id IS NOT NULL",
            name="ck_legal_rules_requires_source_provenance",
        ),
        CheckConstraint(
            "review_status IN ("
            + ", ".join(f"'{s}'" for s in LEGAL_RULE_REVIEW_STATUSES)
            + ")",
            name="ck_legal_rules_review_status",
        ),
        CheckConstraint(
            "verification_status IN ("
            + ", ".join(f"'{s}'" for s in LEGAL_RULE_VERIFICATION_STATUSES)
            + ")",
            name="ck_legal_rules_verification_status",
        ),
        CheckConstraint(
            "verification_status <> 'human_verified' OR ("
            "review_status = 'approved' "
            "AND reviewed_by IS NOT NULL "
            "AND length(trim(reviewed_by)) > 0 "
            "AND reviewed_at IS NOT NULL "
            "AND document_version_id IS NOT NULL"
            ")",
            name="ck_legal_rules_human_verified_requires_audit",
        ),
        Index(
            "ix_legal_rules_lookup",
            "jurisdiction_code",
            "pathway_id",
            "requirement_category",
            "effective_from",
            "effective_to",
        ),
        Index("ix_legal_rules_rule_key", "rule_key"),
        Index("ix_legal_rules_document_version_id", "document_version_id"),
        Index("ix_legal_rules_document_id", "document_id"),
        Index("ix_legal_rules_source_id", "source_id"),
        Index("ix_legal_rules_verification_status", "verification_status", "review_status"),
        Index("ix_legal_rules_effective_dates", "effective_from", "effective_to"),
        Index("ix_legal_rules_cohort", "pathway_id", "cohort_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_key: Mapped[str] = mapped_column(Text, nullable=False)
    rule_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    supersedes_rule_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("legal_rules.id"), nullable=True
    )
    jurisdiction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jurisdictions.id"), nullable=False
    )
    # Denormalized from jurisdictions.code; kept consistent by DB trigger.
    jurisdiction_code: Mapped[str] = mapped_column(Text, nullable=False)
    pathway_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requirement_category: Mapped[str] = mapped_column(Text, nullable=False)
    requirement_type: Mapped[str] = mapped_column(Text, nullable=False)

    # Exact quantitative requirements (never float).
    numeric_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 4, asdecimal=True), nullable=True
    )
    numeric_max_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 4, asdecimal=True), nullable=True
    )
    numeric_comparator: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    currency_code: Mapped[Optional[str]] = mapped_column(CHAR(3), nullable=True)
    amount_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Legal applicability window (explicitly distinct from crawl/publication dates).
    effective_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    effective_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    effective_from_inclusive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    effective_to_inclusive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    effective_date_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="unknown", server_default=text("'unknown'")
    )

    # Cohort / transitional applicability.
    cohort_key: Mapped[str] = mapped_column(
        Text, nullable=False, default="general", server_default=text("'general'")
    )
    cohort_category: Mapped[str] = mapped_column(
        Text, nullable=False, default="general", server_default=text("'general'")
    )
    is_transitional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    cohort_conditions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    rule_summary: Mapped[str] = mapped_column(Text, nullable=False)
    conditions_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Official-source provenance.
    source_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("sources.id"), nullable=True
    )
    document_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("documents.id"), nullable=True
    )
    document_version_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("document_versions.id"), nullable=True
    )
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    supporting_excerpt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Governance & verification state (fail-closed defaults).
    review_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="unreviewed", server_default=text("'unreviewed'")
    )
    verification_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="unverified", server_default=text("'unverified'")
    )
    reviewed_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verification_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    jurisdiction: Mapped[Jurisdiction] = relationship()
    source: Mapped[Optional[Source]] = relationship()
    document: Mapped[Optional[Document]] = relationship()
    document_version: Mapped[Optional[DocumentVersion]] = relationship(
        back_populates="legal_rules",
        foreign_keys=[document_version_id],
    )
    supersedes_rule: Mapped[Optional["LegalRule"]] = relationship(
        remote_side="LegalRule.id",
        foreign_keys=[supersedes_rule_id],
    )


class LegalReviewItem(Base):
    """Persistent human-review work item (Phase 10D.1).

    Fail-closed: creating a row never implies human verification of a LegalRule
    or claim. Approval transitions that mutate LegalRule.verification_status are
    deferred to later 10D phases; this table only records review workflow state.
    """

    __tablename__ = "legal_review_items"
    __table_args__ = (
        CheckConstraint(
            "subject_type IN ("
            + ", ".join(f"'{t}'" for t in LEGAL_REVIEW_SUBJECT_TYPES)
            + ")",
            name="ck_legal_review_items_subject_type",
        ),
        CheckConstraint(
            "workflow_state IN ("
            + ", ".join(f"'{s}'" for s in LEGAL_REVIEW_WORKFLOW_STATES)
            + ")",
            name="ck_legal_review_items_workflow_state",
        ),
        CheckConstraint(
            "risk_class IN ("
            + ", ".join(f"'{r}'" for r in LEGAL_REVIEW_RISK_CLASSES)
            + ")",
            name="ck_legal_review_items_risk_class",
        ),
        CheckConstraint(
            "priority IN ("
            + ", ".join(f"'{p}'" for p in LEGAL_REVIEW_PRIORITIES)
            + ")",
            name="ck_legal_review_items_priority",
        ),
        CheckConstraint(
            "length(trim(review_reason)) > 0",
            name="ck_legal_review_items_review_reason_nonempty",
        ),
        # Must pin examined evidence somehow.
        CheckConstraint(
            "document_version_id IS NOT NULL OR ("
            "evidence_fingerprint IS NOT NULL AND length(trim(evidence_fingerprint)) > 0"
            ")",
            name="ck_legal_review_items_requires_evidence_pin",
        ),
        # legal_rule subjects must reference a LegalRule and pin a DocumentVersion.
        CheckConstraint(
            "subject_type <> 'legal_rule' OR ("
            "legal_rule_id IS NOT NULL AND document_version_id IS NOT NULL"
            ")",
            name="ck_legal_review_items_legal_rule_requires_rule_and_version",
        ),
        CheckConstraint(
            "assigned_actor_kind IS NULL OR assigned_actor_kind IN ("
            + ", ".join(f"'{k}'" for k in LEGAL_REVIEW_ACTOR_KINDS)
            + ")",
            name="ck_legal_review_items_assigned_actor_kind",
        ),
        CheckConstraint(
            "(assigned_actor_id IS NULL AND assigned_actor_kind IS NULL) OR ("
            "assigned_actor_id IS NOT NULL AND length(trim(assigned_actor_id)) > 0 "
            "AND assigned_actor_kind IS NOT NULL"
            ")",
            name="ck_legal_review_items_assigned_actor_pair",
        ),
        CheckConstraint(
            "row_version >= 1",
            name="ck_legal_review_items_row_version_positive",
        ),
        Index("ix_legal_review_items_workflow_state", "workflow_state"),
        Index("ix_legal_review_items_jurisdiction_id", "jurisdiction_id"),
        Index("ix_legal_review_items_pathway_id", "pathway_id"),
        Index("ix_legal_review_items_legal_rule_id", "legal_rule_id"),
        Index("ix_legal_review_items_document_version_id", "document_version_id"),
        Index("ix_legal_review_items_priority_state", "priority", "workflow_state"),
        Index("ix_legal_review_items_assigned_actor_id", "assigned_actor_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        unique=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    legal_rule_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("legal_rules.id"), nullable=True
    )
    # Opaque subject key for claim_evidence / high_risk_queue_item bridges.
    subject_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    jurisdiction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jurisdictions.id"), nullable=False
    )
    pathway_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requirement_category: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    risk_class: Mapped[str] = mapped_column(
        Text, nullable=False, default="high", server_default=text("'high'")
    )
    workflow_state: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default=text("'pending'")
    )
    review_reason: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(
        Text, nullable=False, default="p1", server_default=text("'p1'")
    )

    source_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("sources.id"), nullable=True
    )
    document_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("documents.id"), nullable=True
    )
    document_version_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("document_versions.id"), nullable=True
    )
    evidence_fingerprint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewed_effective_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    reviewed_effective_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    reviewed_cohort_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    assigned_actor_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    assigned_actor_kind: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    review_deadline_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reassessment_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    invalidated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    invalidation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_approved_event_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )

    # Optimistic concurrency for append/update of workflow state.
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
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

    jurisdiction: Mapped[Jurisdiction] = relationship()
    legal_rule: Mapped[Optional[LegalRule]] = relationship()
    source: Mapped[Optional[Source]] = relationship()
    document: Mapped[Optional[Document]] = relationship()
    document_version: Mapped[Optional[DocumentVersion]] = relationship()
    events: Mapped[list["LegalReviewEvent"]] = relationship(
        back_populates="review_item",
        order_by="LegalReviewEvent.id",
        cascade="save-update",
    )


class LegalReviewEvent(Base):
    """Append-only review history row (Phase 10D.1).

    Application code must only INSERT. DB triggers reject UPDATE/DELETE.
    Administrative recovery requires explicit DBA procedures (documented in
    Phase 10D.1 report), not silent application rewrites.
    """

    __tablename__ = "legal_review_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ("
            + ", ".join(f"'{t}'" for t in LEGAL_REVIEW_EVENT_TYPES)
            + ")",
            name="ck_legal_review_events_event_type",
        ),
        CheckConstraint(
            "actor_kind IN ("
            + ", ".join(f"'{k}'" for k in LEGAL_REVIEW_ACTOR_KINDS)
            + ")",
            name="ck_legal_review_events_actor_kind",
        ),
        CheckConstraint(
            "length(trim(actor_id)) > 0",
            name="ck_legal_review_events_actor_id_nonempty",
        ),
        CheckConstraint(
            "from_workflow_state IS NULL OR from_workflow_state IN ("
            + ", ".join(f"'{s}'" for s in LEGAL_REVIEW_WORKFLOW_STATES)
            + ")",
            name="ck_legal_review_events_from_state",
        ),
        CheckConstraint(
            "to_workflow_state IN ("
            + ", ".join(f"'{s}'" for s in LEGAL_REVIEW_WORKFLOW_STATES)
            + ")",
            name="ck_legal_review_events_to_state",
        ),
        CheckConstraint(
            "jsonb_typeof(metadata) = 'object'",
            name="ck_legal_review_events_metadata_object",
        ),
        Index("ix_legal_review_events_review_item_id", "review_item_id"),
        Index("ix_legal_review_events_event_type", "event_type"),
        Index("ix_legal_review_events_occurred_at", "occurred_at"),
        Index("ix_legal_review_events_actor_id", "actor_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    review_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("legal_review_items.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    from_workflow_state: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    to_workflow_state: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence_fingerprint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    document_version_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("document_versions.id"), nullable=True
    )
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    review_item: Mapped[LegalReviewItem] = relationship(back_populates="events")
    document_version: Mapped[Optional[DocumentVersion]] = relationship()
