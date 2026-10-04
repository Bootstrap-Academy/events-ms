"""Prospective discovery on the existing exact subject guard, no historical backfill."""

from alembic import op

import sqlalchemy as sa


revision = "l3eventbook001"
down_revision = "l3eventbenefit002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events_subject_guards", sa.Column("booking_reservations", sa.JSON(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Retain booking discovery until related obligation/erasure evidence is reviewed")
