"""create ratings

Revision ID: ca2b851d688e
Create Date: 2022-12-04 11:40:04.716672
"""

from alembic import op

import sqlalchemy as sa

from api.database.database import UTCDateTime


# revision identifiers, used by Alembic.
revision = "ca2b851d688e"
down_revision = "88370de0c785"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events_lecturer_rating",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lecturer_id", sa.String(length=36), nullable=True),
        sa.Column("participant_id", sa.String(length=36), nullable=True),
        sa.Column("skill_id", sa.String(length=256), nullable=True),
        sa.Column("webinar_timestamp", UTCDateTime(), nullable=True),
        sa.Column("webinar_name", sa.String(length=256), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("events_lecturer_rating")
