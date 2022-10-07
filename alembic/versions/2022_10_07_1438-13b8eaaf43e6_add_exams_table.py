"""add exams table

Revision ID: 13b8eaaf43e6
Create Date: 2022-10-07 14:38:45.933347
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "13b8eaaf43e6"
down_revision = "2d2504573a89"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_exams",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("skill_id", sa.String(length=256), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "skill_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("events_exams")
