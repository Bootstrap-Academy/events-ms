"""Exact existing-event continuation and local target erasure serialization."""
from alembic import op
import sqlalchemy as sa

revision = "l3eventgrants001"
down_revision = "l3eventrights001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_subject_guards",
        sa.Column("subject", sa.String(36), primary_key=True),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        mysql_collate="utf8mb4_bin",
    )
    # This is an explicit local erasure record, not inference from remote404.
    op.execute("INSERT INTO events_subject_guards(subject,deleted) "
               "SELECT subject,true FROM events_commercial_erasure_receipts WHERE erased_at IS NOT NULL")
    op.create_table(
        "events_right_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("right_id", sa.String(36), sa.ForeignKey("events_retained_rights.id"), nullable=False, index=True),
        sa.Column("subject", sa.String(36), nullable=False, index=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    raise RuntimeError("Existing event grant evidence requires reviewed preservation")
