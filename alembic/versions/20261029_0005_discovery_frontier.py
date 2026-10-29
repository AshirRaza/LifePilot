"""persistent discovery frontier for resumable BFS crawl

Revision ID: 20261029_0005
Revises: 20261029_0004
Create Date: 2026-10-29

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261029_0005"
down_revision: Union[str, Sequence[str], None] = "20261029_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "discovery_frontier",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("url_normalized", sa.Text(), nullable=False),
        sa.Column("url_original", sa.Text(), nullable=True),
        sa.Column("parent_url", sa.Text(), nullable=True),
        sa.Column("discovery_method", sa.Text(), nullable=False),
        sa.Column("depth", sa.Integer(), server_default="0", nullable=False),
        sa.Column("content_kind_hint", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("failure_class", sa.Text(), nullable=True),
        sa.Column("document_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "discovered_at",
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
            "discovery_method IN ('seed', 'html_link', 'sitemap')",
            name="ck_discovery_frontier_method",
        ),
        sa.CheckConstraint(
            "status IN ("
            "'pending', 'fetched', 'skipped', 'blocked', 'failed', 'out_of_scope'"
            ")",
            name="ck_discovery_frontier_status",
        ),
        sa.CheckConstraint(
            "failure_class IS NULL OR failure_class IN ('transient', 'permanent')",
            name="ck_discovery_frontier_failure_class",
        ),
        sa.CheckConstraint("depth >= 0", name="ck_discovery_frontier_depth"),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_discovery_frontier_attempt_count"
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "url_normalized", name="uq_discovery_frontier_source_url"
        ),
    )
    op.create_index(
        "ix_discovery_frontier_claim",
        "discovery_frontier",
        ["source_id", "status", "depth", "id"],
    )
    op.create_index(
        "ix_discovery_frontier_document_id",
        "discovery_frontier",
        ["document_id"],
    )
    op.create_index(
        "ix_discovery_frontier_next_attempt_at",
        "discovery_frontier",
        ["next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_discovery_frontier_next_attempt_at", table_name="discovery_frontier"
    )
    op.drop_index("ix_discovery_frontier_document_id", table_name="discovery_frontier")
    op.drop_index("ix_discovery_frontier_claim", table_name="discovery_frontier")
    op.drop_table("discovery_frontier")
