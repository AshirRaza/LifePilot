"""Add last_checked_at for unchanged freshness rechecks.

Revision ID: 20261113_0010
Revises: 20261107_0009
Create Date: 2026-11-13

Phase 9C.5: distinguish successful unchanged rechecks from original
retrieval provenance. Does not alter content_sha256 or retrieved_at.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261113_0010"
down_revision: Union[str, Sequence[str], None] = "20261107_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "document_versions",
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_document_versions_last_checked_at",
        "document_versions",
        ["last_checked_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_versions_last_checked_at", table_name="document_versions"
    )
    op.drop_column("document_versions", "last_checked_at")
