"""Preserve historical guesses as evidence, never as proven charges.

Revision ID: d9b001pay001
"""

from datetime import datetime
from uuid import uuid4

from alembic import op

import sqlalchemy as sa


revision = "d9b001pay001"
down_revision = "c6e1700ab001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_booking_payments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False, index=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, index=True),
        sa.Column("quoted_coins", sa.BigInteger(), nullable=True),
        sa.Column("paid_coins", sa.BigInteger(), nullable=True),
        sa.Column("payout_coins", sa.BigInteger(), nullable=True),
        sa.Column("payout_ratio", sa.String(32), nullable=True),
        sa.Column("description", sa.String(4096), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("original", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("ledger_transaction_id", sa.String(36), nullable=True, unique=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(80), nullable=True),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "events_settlement_claims",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("events_settlement_batches.id"), nullable=False, index=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("payment_ids", sa.JSON(), nullable=False),
        sa.Column("amount_field", sa.String(16), nullable=False),
        sa.Column("ratio", sa.String(32), nullable=False),
        sa.Column("description", sa.String(4096), nullable=False),
        sa.Column("credit_note", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("coins", sa.BigInteger(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True, index=True),
        mysql_collate="utf8mb4_bin",
    )
    op.add_column(
        "events_coin_operations", sa.Column("provenance", sa.String(24), nullable=False, server_default="legacy_hold")
    )
    op.alter_column("events_coin_operations", "provenance", server_default=None)
    op.execute("UPDATE events_coin_operations SET provenance='legacy_completed' WHERE completed_at IS NOT NULL")
    op.add_column("events_webinar_participants", sa.Column("payment_id", sa.String(36), nullable=True, unique=True))
    op.add_column("events_slot", sa.Column("payment_id", sa.String(36), nullable=True, unique=True))
    op.alter_column("events_webinar_participants", "paid_coins", existing_type=sa.BigInteger(), nullable=True)
    conn = op.get_bind()
    payments = sa.table(
        "events_booking_payments",
        *[
            sa.column(name, type_)
            for name, type_ in [
                ("id", sa.String()),
                ("event_id", sa.String()),
                ("user_id", sa.String()),
                ("kind", sa.String()),
                ("state", sa.String()),
                ("quoted_coins", sa.BigInteger()),
                ("description", sa.String()),
                ("original", sa.JSON()),
                ("created_at", sa.DateTime()),
                ("attempts", sa.Integer()),
            ]
        ],
    )
    # No deploy-date inference: every old row lacks a trustworthy booking/payment link.
    # Preserve even zero guesses and post-bbe registrations; no entitlement row is removed.
    rows = (
        conn.execute(
            sa.text(
                "SELECT p.webinar_id, p.user_id, p.paid_coins, w.name, w.price, w.creator, w.start "
                "FROM events_webinar_participants p LEFT JOIN events_webinars w ON w.id = p.webinar_id"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        payment_id = str(uuid4())
        conn.execute(
            payments.insert().values(
                id=payment_id,
                event_id=row["webinar_id"],
                user_id=row["user_id"],
                kind="webinar",
                state="legacy_unknown",
                quoted_coins=None,
                description=f"Webinar '{row['name']}'",
                created_at=datetime.utcnow(),
                attempts=0,
                original={
                    "paid_coins": row["paid_coins"],
                    "current_price": row["price"],
                    "name": row["name"],
                    "lecturer_id": row["creator"],
                    "start": str(row["start"]),
                    "source_revision": down_revision,
                },
            )
        )
        conn.execute(
            sa.text(
                "UPDATE events_webinar_participants SET payment_id=:id, paid_coins=NULL "
                "WHERE webinar_id=:event AND user_id=:user"
            ),
            {"id": payment_id, "event": row["webinar_id"], "user": row["user_id"]},
        )
    # Older coaching code also stored list price for emergency/free bookings.
    rows = (
        conn.execute(
            sa.text(
                "SELECT id, user_id, booked_by, student_coins, instructor_coins, start FROM events_slot "
                "WHERE booked_by IS NOT NULL AND event_type='COACHING'"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        payment_id = str(uuid4())
        conn.execute(
            payments.insert().values(
                id=payment_id,
                event_id=row["id"],
                user_id=row["booked_by"],
                kind="coaching",
                state="legacy_unknown",
                quoted_coins=None,
                description="Coaching",
                created_at=datetime.utcnow(),
                attempts=0,
                original={
                    "student_coins": row["student_coins"],
                    "instructor_coins": row["instructor_coins"],
                    "lecturer_id": row["user_id"],
                    "start": str(row["start"]),
                    "source_revision": down_revision,
                },
            )
        )
        conn.execute(
            sa.text("UPDATE events_slot SET payment_id=:id, student_coins=NULL, instructor_coins=NULL WHERE id=:event"),
            {"id": payment_id, "event": row["id"]},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for table in ("events_booking_payments", "events_settlement_claims"):
        if conn.execute(sa.select(sa.literal(1)).select_from(sa.table(table)).limit(1)).first():
            raise RuntimeError("Cannot remove booking payment/claim evidence; use a forward repair")
    if conn.execute(sa.text("SELECT 1 FROM events_webinar_participants LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade active booking provenance; use a forward repair")
    if conn.execute(sa.text("SELECT 1 FROM events_coin_operations LIMIT 1")).first():
        raise RuntimeError("Cannot remove settlement provenance holds; use a forward repair")
    op.drop_column("events_coin_operations", "provenance")
    op.drop_column("events_slot", "payment_id")
    op.drop_column("events_webinar_participants", "payment_id")
    op.alter_column("events_webinar_participants", "paid_coins", existing_type=sa.BigInteger(), nullable=False)
    op.drop_table("events_settlement_claims")
    op.drop_table("events_booking_payments")
