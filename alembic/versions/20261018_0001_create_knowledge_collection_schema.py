"""create knowledge collection schema

Revision ID: 20261018_0001
Revises:
Create Date: 2026-10-18

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20261018_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "jurisdictions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("jurisdiction_type", sa.Text(), nullable=False),
        sa.Column("country_code", sa.CHAR(length=2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jurisdiction_type IN ('supranational', 'country', 'state', "
            "'province', 'region', 'municipality', 'other')",
            name="ck_jurisdictions_type",
        ),
        sa.ForeignKeyConstraint(["parent_id"], ["jurisdictions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_jurisdictions_parent_id", "jurisdictions", ["parent_id"])
    op.create_index("ix_jurisdictions_country_code", "jurisdictions", ["country_code"])
    op.create_index(
        "ix_jurisdictions_jurisdiction_type", "jurisdictions", ["jurisdiction_type"]
    )

    op.create_table(
        "authorities",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("jurisdiction_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("short_name", sa.Text(), nullable=True),
        sa.Column("website_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "jurisdiction_id", "name", name="uq_authorities_jurisdiction_name"
        ),
    )
    op.create_index(
        "ix_authorities_jurisdiction_id", "authorities", ["jurisdiction_id"]
    )

    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("authority_id", sa.BigInteger(), nullable=False),
        sa.Column("jurisdiction_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("seed_url", sa.Text(), nullable=False),
        sa.Column("seed_url_normalized", sa.Text(), nullable=False),
        sa.Column("allowed_url_patterns", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("review_status", sa.Text(), nullable=False),
        sa.Column("default_language", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "review_status IN ('approved', 'candidate', 'rejected')",
            name="ck_sources_review_status",
        ),
        sa.ForeignKeyConstraint(["authority_id"], ["authorities.id"]),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seed_url_normalized"),
    )
    op.create_index("ix_sources_authority_id", "sources", ["authority_id"])
    op.create_index("ix_sources_jurisdiction_id", "sources", ["jurisdiction_id"])
    op.create_index("ix_sources_review_status", "sources", ["review_status"])

    op.create_table(
        "documents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("authority_id", sa.BigInteger(), nullable=True),
        sa.Column("primary_source_id", sa.BigInteger(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("url_normalized", sa.Text(), nullable=False),
        sa.Column("content_kind", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("translation_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("discovery_status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "content_kind IN ('html_page', 'pdf', 'docx', 'other')",
            name="ck_documents_content_kind",
        ),
        sa.CheckConstraint(
            "discovery_status IN ('seed', 'discovered', 'approved', 'rejected')",
            name="ck_documents_discovery_status",
        ),
        sa.ForeignKeyConstraint(["authority_id"], ["authorities.id"]),
        sa.ForeignKeyConstraint(["primary_source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url_normalized"),
    )
    op.create_index("ix_documents_primary_source_id", "documents", ["primary_source_id"])
    op.create_index("ix_documents_authority_id", "documents", ["authority_id"])
    op.create_index(
        "ix_documents_translation_group_id", "documents", ["translation_group_id"]
    )
    op.create_index("ix_documents_discovery_status", "documents", ["discovery_status"])
    op.create_index("ix_documents_content_kind", "documents", ["content_kind"])

    op.create_table(
        "document_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("fetched_via_source_id", sa.BigInteger(), nullable=True),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["fetched_via_source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id",
            "version_number",
            name="uq_document_versions_document_version",
        ),
    )
    op.create_index(
        "uq_document_versions_one_current",
        "document_versions",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("is_current IS TRUE"),
    )
    op.create_index(
        "ix_document_versions_document_retrieved",
        "document_versions",
        ["document_id", "retrieved_at"],
    )
    op.create_index(
        "ix_document_versions_content_sha256",
        "document_versions",
        ["content_sha256"],
    )
    op.create_index(
        "ix_document_versions_fetched_via_source_id",
        "document_versions",
        ["fetched_via_source_id"],
    )

    op.create_table(
        "document_jurisdictions",
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("jurisdiction_id", sa.BigInteger(), nullable=False),
        sa.Column("relevance", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "relevance IN ('applicable', 'related')",
            name="ck_document_jurisdictions_relevance",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdictions.id"]),
        sa.PrimaryKeyConstraint("document_id", "jurisdiction_id"),
    )
    op.create_index(
        "ix_document_jurisdictions_jurisdiction_id",
        "document_jurisdictions",
        ["jurisdiction_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_jurisdictions_jurisdiction_id",
        table_name="document_jurisdictions",
    )
    op.drop_table("document_jurisdictions")

    op.drop_index(
        "ix_document_versions_fetched_via_source_id",
        table_name="document_versions",
    )
    op.drop_index(
        "ix_document_versions_content_sha256", table_name="document_versions"
    )
    op.drop_index(
        "ix_document_versions_document_retrieved", table_name="document_versions"
    )
    op.drop_index(
        "uq_document_versions_one_current",
        table_name="document_versions",
        postgresql_where=sa.text("is_current IS TRUE"),
    )
    op.drop_table("document_versions")

    op.drop_index("ix_documents_content_kind", table_name="documents")
    op.drop_index("ix_documents_discovery_status", table_name="documents")
    op.drop_index("ix_documents_translation_group_id", table_name="documents")
    op.drop_index("ix_documents_authority_id", table_name="documents")
    op.drop_index("ix_documents_primary_source_id", table_name="documents")
    op.drop_table("documents")

    op.drop_index("ix_sources_review_status", table_name="sources")
    op.drop_index("ix_sources_jurisdiction_id", table_name="sources")
    op.drop_index("ix_sources_authority_id", table_name="sources")
    op.drop_table("sources")

    op.drop_index("ix_authorities_jurisdiction_id", table_name="authorities")
    op.drop_table("authorities")

    op.drop_index("ix_jurisdictions_jurisdiction_type", table_name="jurisdictions")
    op.drop_index("ix_jurisdictions_country_code", table_name="jurisdictions")
    op.drop_index("ix_jurisdictions_parent_id", table_name="jurisdictions")
    op.drop_table("jurisdictions")
