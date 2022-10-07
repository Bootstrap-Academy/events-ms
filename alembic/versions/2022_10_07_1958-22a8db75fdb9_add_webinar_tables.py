"""add webinar tables

Revision ID: 22a8db75fdb9
Create Date: 2022-10-07 19:58:42.795614
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "22a8db75fdb9"
down_revision = "696a7f0879a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_webinars",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("skill_id", sa.String(length=256), nullable=True),
        sa.Column("creator", sa.String(length=36), nullable=True),
        sa.Column("creation_date", sa.DateTime(), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=True),
        sa.Column("description", sa.String(length=4096), nullable=True),
        sa.Column("link", sa.String(length=256), nullable=True),
        sa.Column("start", sa.DateTime(), nullable=True),
        sa.Column("end", sa.DateTime(), nullable=True),
        sa.Column("max_participants", sa.Integer(), nullable=True),
        sa.Column("price", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "events_webinar_participants",
        sa.Column("webinar_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["webinar_id"], ["events_webinars.id"]),
        sa.PrimaryKeyConstraint("webinar_id", "user_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("events_webinar_participants")
    op.drop_table("events_webinars")
