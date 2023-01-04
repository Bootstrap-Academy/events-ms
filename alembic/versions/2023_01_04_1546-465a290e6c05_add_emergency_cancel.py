"""Add emergency_cancel

Revision ID: 465a290e6c05
Create Date: 2023-01-04 15:46:29.293023
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "465a290e6c05"
down_revision = "5066fdf92e3c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_emergency_cancel",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("user_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("events_emergency_cancel")
