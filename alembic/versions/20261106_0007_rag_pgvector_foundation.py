"""Enable pgvector and add additive RAG chunk/embedding schema.

Revision ID: 20261106_0007
Revises: 20261030_0006
Create Date: 2026-11-06

Phase 8B: schema foundation only — does not populate chunks or embeddings.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "20261106_0007"
down_revision: Union[str, Sequence[str], None] = "20261030_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PLANNED_MODEL_ID = "intfloat/multilingual-e5-small"
PLANNED_DIMS = 384


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "embedding_models",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column(
            "distance_metric",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'cosine'"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("dimensions > 0", name="ck_embedding_models_dimensions"),
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id"),
            nullable=False,
        ),
        sa.Column(
            "document_version_id",
            sa.BigInteger(),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.BigInteger(),
            sa.ForeignKey("sources.id"),
            nullable=False,
        ),
        sa.Column(
            "jurisdiction_id",
            sa.BigInteger(),
            sa.ForeignKey("jurisdictions.id"),
            nullable=False,
        ),
        sa.Column("jurisdiction_code", sa.Text(), nullable=False),
        sa.Column("pathway_id", sa.Text(), nullable=True),
        sa.Column("requirement_category", sa.Text(), nullable=True),
        sa.Column("heading_path", sa.Text(), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("is_current_version", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "chunk_index >= 0", name="ck_document_chunks_chunk_index"
        ),
        sa.CheckConstraint(
            "length(trim(content_text)) > 0",
            name="ck_document_chunks_content_nonempty",
        ),
        sa.CheckConstraint(
            "char_start IS NULL OR char_start >= 0",
            name="ck_document_chunks_char_start",
        ),
        sa.CheckConstraint(
            "char_end IS NULL OR char_start IS NULL OR char_end >= char_start",
            name="ck_document_chunks_char_range",
        ),
        sa.CheckConstraint(
            "requirement_category IS NULL OR requirement_category IN ("
            "'eligibility', 'required_documents', 'application_process', "
            "'financial_or_sponsorship', 'official_provenance')",
            name="ck_document_chunks_requirement_category",
        ),
        sa.CheckConstraint(
            "pathway_id IS NULL OR pathway_id ~ '^[a-z][a-z0-9_]*$'",
            name="ck_document_chunks_pathway_id",
        ),
        sa.CheckConstraint(
            "length(trim(jurisdiction_code)) > 0",
            name="ck_document_chunks_jurisdiction_code_nonempty",
        ),
        sa.UniqueConstraint(
            "document_version_id",
            "chunk_index",
            name="uq_document_chunks_version_index",
        ),
        sa.UniqueConstraint(
            "document_version_id",
            "content_sha256",
            name="uq_document_chunks_version_content_hash",
        ),
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])
    op.create_index(
        "ix_document_chunks_document_version_id",
        "document_chunks",
        ["document_version_id"],
    )
    op.create_index("ix_document_chunks_source_id", "document_chunks", ["source_id"])
    op.create_index(
        "ix_document_chunks_jurisdiction_id", "document_chunks", ["jurisdiction_id"]
    )
    op.create_index(
        "ix_document_chunks_jurisdiction_current",
        "document_chunks",
        ["jurisdiction_code", "is_current_version"],
    )
    op.create_index("ix_document_chunks_pathway_id", "document_chunks", ["pathway_id"])
    op.create_index(
        "ix_document_chunks_content_sha256", "document_chunks", ["content_sha256"]
    )
    op.execute(
        """
        CREATE INDEX ix_document_chunks_retrievable
        ON document_chunks (jurisdiction_code)
        WHERE is_current_version IS TRUE
        """
    )

    # Keep denormalized jurisdiction_code aligned with jurisdictions.code.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION rag_enforce_chunk_jurisdiction_code()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            expected_code text;
        BEGIN
            SELECT code INTO expected_code
            FROM jurisdictions
            WHERE id = NEW.jurisdiction_id;
            IF expected_code IS NULL THEN
                RAISE EXCEPTION
                    'document_chunks.jurisdiction_id % does not exist',
                    NEW.jurisdiction_id;
            END IF;
            IF NEW.jurisdiction_code IS DISTINCT FROM expected_code THEN
                RAISE EXCEPTION
                    'document_chunks.jurisdiction_code % does not match jurisdictions.code % for id %',
                    NEW.jurisdiction_code, expected_code, NEW.jurisdiction_id;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_document_chunks_jurisdiction_code
        BEFORE INSERT OR UPDATE OF jurisdiction_id, jurisdiction_code
        ON document_chunks
        FOR EACH ROW
        EXECUTE FUNCTION rag_enforce_chunk_jurisdiction_code();
        """
    )

    op.create_table(
        "document_chunk_embeddings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "chunk_id",
            sa.BigInteger(),
            sa.ForeignKey("document_chunks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "embedding_model_id",
            sa.Text(),
            sa.ForeignKey("embedding_models.id"),
            nullable=False,
        ),
        sa.Column("embedding", Vector(PLANNED_DIMS), nullable=False),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "chunk_id",
            "embedding_model_id",
            name="uq_document_chunk_embeddings_chunk_model",
        ),
    )
    op.create_index(
        "ix_document_chunk_embeddings_model_id",
        "document_chunk_embeddings",
        ["embedding_model_id"],
    )
    op.create_index(
        "ix_document_chunk_embeddings_indexed_at",
        "document_chunk_embeddings",
        ["indexed_at"],
    )

    # Only 384-d models may write into this vector(384) column.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION rag_enforce_embedding_dimensions()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            model_dims integer;
        BEGIN
            SELECT dimensions INTO model_dims
            FROM embedding_models
            WHERE id = NEW.embedding_model_id;
            IF model_dims IS NULL THEN
                RAISE EXCEPTION
                    'embedding_model_id % is not registered',
                    NEW.embedding_model_id;
            END IF;
            IF model_dims <> 384 THEN
                RAISE EXCEPTION
                    'document_chunk_embeddings only stores 384-d vectors; model % has dimensions=% — add a new table/column for other sizes',
                    NEW.embedding_model_id, model_dims;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_document_chunk_embeddings_dimensions
        BEFORE INSERT OR UPDATE OF embedding_model_id, embedding
        ON document_chunk_embeddings
        FOR EACH ROW
        EXECUTE FUNCTION rag_enforce_embedding_dimensions();
        """
    )

    # HNSW index ready for Phase 8C population (safe on empty table).
    op.execute(
        """
        CREATE INDEX ix_document_chunk_embeddings_hnsw
        ON document_chunk_embeddings
        USING hnsw (embedding vector_cosine_ops)
        """
    )

    op.execute(
        sa.text(
            """
            INSERT INTO embedding_models (id, display_name, dimensions, distance_metric, notes)
            VALUES (
                :id,
                'Multilingual E5 Small',
                :dims,
                'cosine',
                'Planned Phase 8C local embedding model. Use query:/passage: prefixes. '
                'A future non-384-d model requires a new embeddings table or column.'
            )
            ON CONFLICT (id) DO NOTHING
            """
        ).bindparams(id=PLANNED_MODEL_ID, dims=PLANNED_DIMS)
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunk_embeddings_hnsw")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_document_chunk_embeddings_dimensions "
        "ON document_chunk_embeddings"
    )
    op.execute("DROP FUNCTION IF EXISTS rag_enforce_embedding_dimensions()")
    op.drop_table("document_chunk_embeddings")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_document_chunks_jurisdiction_code "
        "ON document_chunks"
    )
    op.execute("DROP FUNCTION IF EXISTS rag_enforce_chunk_jurisdiction_code()")
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_retrievable")
    op.drop_table("document_chunks")
    op.drop_table("embedding_models")
    # Leave the vector extension installed — dropping it would fail if other
    # objects depend on it and is unnecessary for safe rollback of this phase.
