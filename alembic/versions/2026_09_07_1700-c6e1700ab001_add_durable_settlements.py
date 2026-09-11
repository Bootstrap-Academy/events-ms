"""Durable event financial operations; historical prices are unchanged.

Revision ID: c6e1700ab001
"""

from alembic import op

import sqlalchemy as sa


revision = "c6e1700ab001"
down_revision = "bbe2621aea9c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_settlement_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), nullable=True, index=True),
        sa.Column("event_id", sa.String(36), nullable=True, index=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("notifications", sa.JSON(), nullable=False),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "events_coin_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("events_settlement_batches.id"), nullable=False, index=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("coins", sa.BigInteger(), nullable=False),
        sa.Column("description", sa.String(4096), nullable=False),
        sa.Column("credit_note", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True, index=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(80), nullable=True),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM events_settlement_batches LIMIT 1")).first():
        raise RuntimeError("Cannot remove event settlement evidence; use a forward repair")
    op.drop_table("events_coin_operations")
    op.drop_table("events_settlement_batches")
