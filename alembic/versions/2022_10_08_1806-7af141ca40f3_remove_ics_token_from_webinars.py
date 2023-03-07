"""remove ics_token from webinars

Revision ID: 7af141ca40f3
Create Date: 2022-10-08 18:06:33.527135
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "7af141ca40f3"
down_revision = "9d224bae5ed1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("events_webinars", "ics_token")


def downgrade() -> None:
    op.add_column("events_webinars", sa.Column("ics_token", sa.String(length=64), nullable=True))
