"""append-only comparison result revisions (supersession)

Revision ID: 20261029_0004
Revises: 20261029_0003
Create Date: 2026-10-29

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261029_0004"
down_revision: Union[str, Sequence[str], None] = "20261029_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "document_content_comparisons",
        sa.Column(
            "result_revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "document_content_comparisons",
        sa.Column("supersedes_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_document_content_comparisons_supersedes_id",
        "document_content_comparisons",
        "document_content_comparisons",
        ["supersedes_id"],
        ["id"],
    )
    op.drop_constraint(
        "uq_document_content_comparisons_pair",
        "document_content_comparisons",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_document_content_comparisons_pair_revision",
        "document_content_comparisons",
        [
            "previous_version_id",
            "current_version_id",
            "extractor_name",
            "extractor_version",
            "algorithm_version",
            "result_revision",
        ],
    )
    op.create_index(
        "ix_document_content_comparisons_supersedes_id",
        "document_content_comparisons",
        ["supersedes_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_content_comparisons_supersedes_id",
        table_name="document_content_comparisons",
    )
    op.drop_constraint(
        "uq_document_content_comparisons_pair_revision",
        "document_content_comparisons",
        type_="unique",
    )
    # Downgrade only safe when each pair has a single revision.
    op.create_unique_constraint(
        "uq_document_content_comparisons_pair",
        "document_content_comparisons",
        [
            "previous_version_id",
            "current_version_id",
            "extractor_name",
            "extractor_version",
            "algorithm_version",
        ],
    )
    op.drop_constraint(
        "fk_document_content_comparisons_supersedes_id",
        "document_content_comparisons",
        type_="foreignkey",
    )
    op.drop_column("document_content_comparisons", "supersedes_id")
    op.drop_column("document_content_comparisons", "result_revision")
