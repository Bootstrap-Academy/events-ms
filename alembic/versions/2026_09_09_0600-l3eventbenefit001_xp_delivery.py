"""Prospective Events XP earning/outbox and exact delivery observations."""

from alembic import op

import sqlalchemy as sa


revision = "l3eventbenefit001"
down_revision = "l3eventgrants001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No backfill: missing old receipts are not evidence of uncredited rewards.
    op.create_table(
        "events_benefits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("source_subject", sa.String(36), nullable=False, index=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("event_id", sa.String(36), nullable=False, index=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original", sa.JSON(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=True),
        sa.UniqueConstraint("source_kind", "source_id", "role"),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "events_benefit_observations",
        sa.Column("benefit_id", sa.String(36), sa.ForeignKey("events_benefits.id"), primary_key=True),
        sa.Column("attempt", sa.Integer(), primary_key=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    raise RuntimeError("Earned benefit evidence requires reviewed preservation")
