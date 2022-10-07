"""add calendly_link to booked_exams

Revision ID: 9d224bae5ed1
Create Date: 2022-10-07 23:24:45.019963
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9d224bae5ed1"
down_revision = "22a8db75fdb9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events_booked_exams", sa.Column("calendly_link", sa.String(length=256), nullable=True))


def downgrade() -> None:
    op.drop_column("events_booked_exams", "calendly_link")
