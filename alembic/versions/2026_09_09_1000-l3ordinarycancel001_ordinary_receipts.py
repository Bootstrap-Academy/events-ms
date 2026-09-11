"""Separate ordinary authenticated declarations; no retained receipt changes/backfill."""

from alembic import op
import sqlalchemy as sa

revision = "l3ordinarycancel001"
down_revision = "l3eventcancel001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "events_ordinary_cancellation_targets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), nullable=False, index=True),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original", sa.JSON(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "events_ordinary_cancellations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), nullable=False, index=True),
        sa.Column("target_id", sa.String(36), sa.ForeignKey("events_ordinary_cancellation_targets.id"), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original", sa.JSON(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("result", sa.JSON(none_as_null=True), nullable=True),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "events_ordinary_cancellation_claim_evidence",
        sa.Column("command_id", sa.String(36), sa.ForeignKey("events_ordinary_cancellations.id"), primary_key=True),
        sa.Column("claim_id", sa.String(36), sa.ForeignKey("events_settlement_claims.id"), primary_key=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assessment", sa.JSON(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )


def downgrade():
    raise RuntimeError("Preserve actual cancellation targets, declarations and original-claim evidence")
