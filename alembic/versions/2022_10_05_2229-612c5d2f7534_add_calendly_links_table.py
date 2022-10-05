"""add calendly_links table

Revision ID: 612c5d2f7534
Create Date: 2022-10-05 22:29:26.112990
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "612c5d2f7534"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_calendly_links",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("api_token", sa.String(length=512), nullable=True),
        sa.Column("uri", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("user_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("events_calendly_links")
