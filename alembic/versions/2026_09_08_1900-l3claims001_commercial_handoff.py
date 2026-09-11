"""Independent original-request and commercial handoff evidence.

Revision ID: l3claims001
Revises: l1events002
"""

from alembic import op
import sqlalchemy as sa

revision = "l3claims001"
down_revision = "l1events002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "events_settlement_claims",
        sa.Column("entitlement", sa.String(32), nullable=False, server_default="legacy_review"),
    )
    op.add_column("events_settlement_claims", sa.Column("basis", sa.JSON(), nullable=False, server_default="{}"))
    op.create_table(
        "events_commercial_handoffs",
        sa.Column("claim_id", sa.String(36), sa.ForeignKey("events_settlement_claims.id"), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(80), nullable=True),
    )
    op.create_table(
        "events_commercial_batch_claims",
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("events_settlement_batches.id"), primary_key=True),
        sa.Column("claim_id", sa.String(36), sa.ForeignKey("events_settlement_claims.id"), primary_key=True),
    )
    op.create_table(
        "events_commercial_erasure_receipts",
        sa.Column("subject", sa.String(36), primary_key=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canonical", sa.JSON(), nullable=True),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    raise RuntimeError("Commercial request/claim evidence requires a reviewed preservation migration")
