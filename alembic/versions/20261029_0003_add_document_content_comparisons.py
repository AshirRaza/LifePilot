"""add document_content_comparisons for meaningful change detection

Revision ID: 20261029_0003
Revises: 20261028_0002
Create Date: 2026-10-29

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261029_0003"
down_revision: Union[str, Sequence[str], None] = "20261028_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_content_comparisons",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("previous_version_id", sa.BigInteger(), nullable=False),
        sa.Column("current_version_id", sa.BigInteger(), nullable=False),
        sa.Column("extractor_name", sa.Text(), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("algorithm_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("previous_content_sha256", sa.CHAR(length=64), nullable=True),
        sa.Column("current_content_sha256", sa.CHAR(length=64), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("unavailable_reason", sa.Text(), nullable=True),
        sa.Column("compared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('unchanged', 'changed', 'unavailable')",
            name="ck_document_content_comparisons_status",
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["previous_version_id"], ["document_versions.id"]),
        sa.ForeignKeyConstraint(["current_version_id"], ["document_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "previous_version_id",
            "current_version_id",
            "extractor_name",
            "extractor_version",
            "algorithm_version",
            name="uq_document_content_comparisons_pair",
        ),
    )
    op.create_index(
        "ix_document_content_comparisons_document_id",
        "document_content_comparisons",
        ["document_id"],
    )
    op.create_index(
        "ix_document_content_comparisons_status",
        "document_content_comparisons",
        ["status"],
    )
    op.create_index(
        "ix_document_content_comparisons_compared_at",
        "document_content_comparisons",
        ["compared_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_content_comparisons_compared_at",
        table_name="document_content_comparisons",
    )
    op.drop_index(
        "ix_document_content_comparisons_status",
        table_name="document_content_comparisons",
    )
    op.drop_index(
        "ix_document_content_comparisons_document_id",
        table_name="document_content_comparisons",
    )
    op.drop_table("document_content_comparisons")
