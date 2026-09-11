"""Explicit cancellation receipts and additive original-claim evidence; no inference/backfill."""

from alembic import op
import sqlalchemy as sa

revision = "l3eventcancel001"
down_revision = "l3eventbook001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "events_cancellation_declarations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_subject", sa.String(36), nullable=False, index=True),
        sa.Column("right_id", sa.String(36), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "events_cancellation_claim_evidence",
        sa.Column("command_id", sa.String(36), sa.ForeignKey("events_cancellation_declarations.id"), primary_key=True),
        sa.Column("claim_id", sa.String(36), sa.ForeignKey("events_settlement_claims.id"), primary_key=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assessment", sa.JSON(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )


def downgrade():
    raise RuntimeError("Preserve original cancellation receipts and claim evidence until reviewed disposition")
