"""add booked_exams table

Revision ID: 696a7f0879a0
Create Date: 2022-10-07 15:13:41.186898
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "696a7f0879a0"
down_revision = "13b8eaaf43e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_booked_exams",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("skill_id", sa.String(length=256), nullable=False),
        sa.Column("examiner_id", sa.String(length=36), nullable=True),
        sa.Column("confirmed", sa.Boolean, nullable=True),
        sa.PrimaryKeyConstraint("user_id", "skill_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("events_booked_exams")
