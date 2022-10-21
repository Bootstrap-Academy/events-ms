"""remove calendly table

Revision ID: ae37eb642276
Create Date: 2022-10-20 11:21:49.682630
"""

from alembic import op

import sqlalchemy as sa
from sqlalchemy.dialects import mysql


# revision identifiers, used by Alembic.
revision = "ae37eb642276"
down_revision = "7af141ca40f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("events_calendly_links")


def downgrade() -> None:
    op.create_table(
        "events_calendly_links",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("api_token", sa.String(length=512), nullable=True),
        sa.Column("uri", sa.String(length=256), nullable=True),
        sa.Column("webhook_signing_key", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("user_id"),
        mysql_collate="utf8mb4_bin",
    )
