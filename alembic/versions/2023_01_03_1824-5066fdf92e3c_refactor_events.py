"""Refactor events

Revision ID: 5066fdf92e3c
Create Date: 2023-01-03 18:24:39.185320
"""

from alembic import op

import sqlalchemy as sa

from api import models


# revision identifiers, used by Alembic.
revision = "5066fdf92e3c"
down_revision = "ca2b851d688e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events_slot", sa.Column("admin_link", sa.String(length=256), nullable=True))
    op.alter_column("events_slot", "meeting_link", new_column_name="link", existing_type=sa.String(256))
    op.execute(sa.update(models.Slot).values(admin_link=models.Slot.link))
    op.add_column("events_webinars", sa.Column("admin_link", sa.String(length=256), nullable=True))


def downgrade() -> None:
    op.drop_column("events_webinars", "admin_link")
    op.alter_column("events_slot", "link", new_column_name="meeting_link", existing_type=sa.String(256))
    op.drop_column("events_slot", "admin_link")
