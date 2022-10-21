"""remove booked_exam table

Revision ID: c9413586ccc8
Create Date: 2022-10-21 18:24:05.964176
"""

from alembic import op

import sqlalchemy as sa
from sqlalchemy.dialects import mysql


# revision identifiers, used by Alembic.
revision = "c9413586ccc8"
down_revision = "05811f006d1f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("events_booked_exams")


def downgrade() -> None:
    op.create_table(
        "events_booked_exams",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("skill_id", sa.String(length=256), nullable=False),
        sa.Column("examiner_id", sa.String(length=36), nullable=True),
        sa.Column("confirmed", sa.Boolean, nullable=True),
        sa.PrimaryKeyConstraint("user_id", "skill_id"),
        mysql_collate="utf8mb4_bin",
    )
