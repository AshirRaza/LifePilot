"""Add full-text search support for document_chunks retrieval.

Revision ID: 20261107_0009
Revises: 20261106_0008
Create Date: 2026-11-07

Phase 8E: keyword search over chunk text + heading_path. Does not alter
chunk content, embeddings, evidence, or source approvals.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20261107_0009"
down_revision: Union[str, Sequence[str], None] = "20261106_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Generated tsvector covering passage text and heading path.
    op.execute(
        """
        ALTER TABLE document_chunks
        ADD COLUMN IF NOT EXISTS search_tsv tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(heading_path, '')), 'A')
            || setweight(to_tsvector('english', coalesce(content_text, '')), 'B')
        ) STORED
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_document_chunks_search_tsv
        ON document_chunks
        USING gin (search_tsv)
        """
    )
    # Speeds jurisdiction-scoped keyword queries on current chunks.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_document_chunks_jurisdiction_current_fts
        ON document_chunks (jurisdiction_code)
        WHERE is_current_version IS TRUE
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_jurisdiction_current_fts")
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_search_tsv")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS search_tsv")
