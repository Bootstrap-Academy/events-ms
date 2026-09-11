"""Explicit prospective XP producer provenance; old state is never presumed unpaid."""
from alembic import op
import sqlalchemy as sa

revision = "l3eventbenefit002"
down_revision = "l3eventbenefit001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No default or backfill: existing sessions/payments remain unproven. Only
    # new owning producers assign protocol1, separate from original contracts.
    op.add_column("events_webinars", sa.Column("xp_delivery_protocol", sa.Integer(), nullable=True))
    op.add_column("events_booking_payments", sa.Column("xp_delivery_protocol", sa.Integer(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Prospective benefit provenance must remain reviewable")
