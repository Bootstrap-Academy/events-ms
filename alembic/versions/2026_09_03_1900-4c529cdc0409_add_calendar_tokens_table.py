"""add calendar_tokens table

Revision ID: 4c529cdc0409
Create Date: 2026-09-03 19:00:00.000000
"""

from alembic import op

import sqlalchemy as sa

from api.database.database import UTCDateTime


# revision identifiers, used by Alembic.
revision = "4c529cdc0409"
down_revision = "465a290e6c05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_calendar_tokens",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("token"),
        sa.UniqueConstraint("user_id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("events_calendar_tokens")
