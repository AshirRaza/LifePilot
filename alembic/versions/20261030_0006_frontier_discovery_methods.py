"""extend discovery_frontier methods for pageschema and exact allowlist

Revision ID: 20261030_0006
Revises: 20261029_0005
Create Date: 2026-10-30

"""

from typing import Sequence, Union

from alembic import op

revision: str = "20261030_0006"
down_revision: Union[str, Sequence[str], None] = "20261029_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_discovery_frontier_method", "discovery_frontier", type_="check"
    )
    op.create_check_constraint(
        "ck_discovery_frontier_method",
        "discovery_frontier",
        "discovery_method IN ("
        "'seed', 'html_link', 'sitemap', 'pageschema_link', 'allowlist_exact'"
        ")",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_discovery_frontier_method", "discovery_frontier", type_="check"
    )
    op.create_check_constraint(
        "ck_discovery_frontier_method",
        "discovery_frontier",
        "discovery_method IN ('seed', 'html_link', 'sitemap')",
    )
