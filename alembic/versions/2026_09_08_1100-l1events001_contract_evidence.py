"""Prospective exact booking offers and independently recoverable confirmations."""

from alembic import op

import sqlalchemy as sa


revision = "l1events001"
down_revision = "d9b001pay001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_booking_contracts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("offer", sa.JSON(), nullable=False),
        sa.Column("fulfillment", sa.JSON(), nullable=True),
        sa.Column("reported", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("acceptance", sa.JSON()),
        sa.Column("outcome", sa.JSON()),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("closed", sa.Boolean(), nullable=False),
    )
    for field in ("user_id", "event_id", "state"):
        op.create_index(f"ix_events_booking_contracts_{field}", "events_booking_contracts", [field])


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM events_booking_contracts WHERE acceptance IS NOT NULL"))
        .scalar()
    ):
        raise RuntimeError("Archive accepted contract evidence before deliberate downgrade")
    op.drop_table("events_booking_contracts")
