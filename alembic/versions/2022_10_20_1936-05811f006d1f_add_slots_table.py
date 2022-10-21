"""add slots table

Revision ID: 05811f006d1f
Create Date: 2022-10-20 19:36:53.183980
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "05811f006d1f"
down_revision = "ae37eb642276"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_slot",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("start", sa.DateTime(), nullable=True),
        sa.Column("end", sa.DateTime(), nullable=True),
        sa.Column("booked_by", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.Enum("COACHING", "EXAM", name="eventtype"), nullable=True),
        sa.Column("student_coins", sa.BigInteger(), nullable=True),
        sa.Column("instructor_coins", sa.BigInteger(), nullable=True),
        sa.Column("skill_id", sa.String(length=256), nullable=True),
        sa.Column("meeting_link", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("events_slot")
