"""add paid_coins to webinar participants

Revision ID: bbe2621aea9c
Create Date: 2026-09-07 12:00:00.000000
"""

from alembic import op

import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "bbe2621aea9c"
down_revision = "4c529cdc0409"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events_webinar_participants", sa.Column("paid_coins", sa.BigInteger(), nullable=True))
    # every registration that exists was charged the price of its webinar, which is what the refund paths used until
    # now, so backfilling with that price keeps the amount they refund unchanged for them
    op.execute(
        "UPDATE events_webinar_participants SET paid_coins = COALESCE("
        "(SELECT price FROM events_webinars WHERE events_webinars.id = events_webinar_participants.webinar_id), 0)"
    )
    op.alter_column("events_webinar_participants", "paid_coins", existing_type=sa.BigInteger(), nullable=False)


def downgrade() -> None:
    op.drop_column("events_webinar_participants", "paid_coins")
