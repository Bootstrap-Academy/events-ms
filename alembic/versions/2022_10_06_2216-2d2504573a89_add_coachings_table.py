"""add coachings table

Revision ID: 2d2504573a89
Create Date: 2022-10-06 22:16:42.741534
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "2d2504573a89"
down_revision = "612c5d2f7534"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_coachings",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("skill_id", sa.String(length=256), nullable=False),
        sa.Column("price", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("user_id", "skill_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("events_coachings")
