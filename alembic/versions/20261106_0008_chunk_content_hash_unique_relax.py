"""Allow repeated identical passages within one document version.

Revision ID: 20261106_0008
Revises: 20261106_0007
Create Date: 2026-11-06

Identical substantive text can legitimately appear twice in one document
(e.g. repeated checklist items). Uniqueness remains on
(document_version_id, chunk_index). content_sha256 stays indexed for
dedupe analytics but is no longer unique per version.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20261106_0008"
down_revision: Union[str, Sequence[str], None] = "20261106_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_document_chunks_version_content_hash",
        "document_chunks",
        type_="unique",
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_document_chunks_version_content_hash",
        "document_chunks",
        ["document_version_id", "content_sha256"],
    )
