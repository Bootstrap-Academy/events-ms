"""add weekly slots

Revision ID: 88370de0c785
Create Date: 2022-10-21 20:22:29.518063
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "88370de0c785"
down_revision = "c9413586ccc8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_weekly_slots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("weekday", sa.SmallInteger(), nullable=True),
        sa.Column("start", sa.Time(), nullable=True),
        sa.Column("end", sa.Time(), nullable=True),
        sa.Column("last_slot", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
        mysql_collate="utf8mb4_bin",
    )
    op.add_column("events_slot", sa.Column("weekly_slot_id", sa.String(length=36), nullable=True))
    op.create_foreign_key("weekly_slot_id", "events_slot", "events_weekly_slots", ["weekly_slot_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("weekly_slot_id", "events_slot", type_="foreignkey")
    op.drop_column("events_slot", "weekly_slot_id")
    op.drop_table("events_weekly_slots")
