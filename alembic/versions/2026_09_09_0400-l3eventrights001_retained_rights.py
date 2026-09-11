"""Existing booking rights survive data-only erasure without inferred cancellation."""

from alembic import op
import sqlalchemy as sa

revision = "l3eventrights001"
down_revision = "l3claims001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "events_webinars", sa.Column("closed_to_new_bookings", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_table(
        "events_retained_rights",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_subject", sa.String(36), nullable=False, index=True),
        sa.Column("event_id", sa.String(36), nullable=False, index=True),
        sa.Column("payment_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original", sa.JSON(), nullable=False),
        sa.Column("current_subject", sa.String(36), nullable=True, index=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.UniqueConstraint("payment_id", "role"),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "events_retained_right_erasures",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("right_id", sa.String(36), nullable=False, index=True),
        sa.Column("subject", sa.String(36), nullable=False, index=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    raise RuntimeError("Existing booking rights require a reviewed preservation migration")
