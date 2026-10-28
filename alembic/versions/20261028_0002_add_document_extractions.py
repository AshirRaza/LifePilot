"""add document_extractions for structured HTML extraction

Revision ID: 20261028_0002
Revises: 20261018_0001
Create Date: 2026-10-28

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261028_0002"
down_revision: Union[str, Sequence[str], None] = "20261018_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_extractions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("document_version_id", sa.BigInteger(), nullable=False),
        sa.Column("extractor_name", sa.Text(), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.CHAR(length=64), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
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
            "status IN ('success', 'empty', 'failed', 'skipped')",
            name="ck_document_extractions_status",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id",
            "extractor_name",
            name="uq_document_extractions_version_extractor",
        ),
    )
    op.create_index(
        "ix_document_extractions_document_version_id",
        "document_extractions",
        ["document_version_id"],
    )
    op.create_index(
        "ix_document_extractions_status",
        "document_extractions",
        ["status"],
    )
    op.create_index(
        "ix_document_extractions_content_sha256",
        "document_extractions",
        ["content_sha256"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_extractions_content_sha256",
        table_name="document_extractions",
    )
    op.drop_index(
        "ix_document_extractions_status",
        table_name="document_extractions",
    )
    op.drop_index(
        "ix_document_extractions_document_version_id",
        table_name="document_extractions",
    )
    op.drop_table("document_extractions")
