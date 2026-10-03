"""Immutable committed-availability candidates and observations."""

from alembic import op

import sqlalchemy as sa


revision = "l1events002"
down_revision = "l1events001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events_booking_contracts", sa.Column("candidate", sa.JSON(), nullable=True))
    op.create_table(
        "events_booking_availability",
        sa.Column("order_id", sa.String(36), primary_key=True),
        sa.Column("proof", sa.JSON(), nullable=False),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """CREATE FUNCTION events_immutable_availability() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Availability evidence is immutable; retention requires reviewed archival'; END $$"""
        )
        op.execute(
            """CREATE TRIGGER immutable_availability BEFORE UPDATE OR DELETE ON events_booking_availability
        FOR EACH ROW EXECUTE FUNCTION events_immutable_availability()"""
        )
        op.execute(
            """CREATE FUNCTION events_immutable_candidate() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF OLD.candidate IS NOT NULL AND OLD.candidate::jsonb IS DISTINCT FROM NEW.candidate::jsonb THEN
        RAISE EXCEPTION 'Availability candidate is immutable'; END IF; RETURN NEW; END $$"""
        )
        op.execute(
            """CREATE TRIGGER immutable_candidate BEFORE UPDATE ON events_booking_contracts
        FOR EACH ROW EXECUTE FUNCTION events_immutable_candidate()"""
        )
    elif dialect == "mysql":
        for event in ("UPDATE", "DELETE"):
            op.execute(
                f"""CREATE TRIGGER immutable_availability_{event.lower()} BEFORE {event}
            ON events_booking_availability FOR EACH ROW SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT='Availability evidence is immutable'"""
            )
        op.execute(
            """CREATE TRIGGER immutable_candidate BEFORE UPDATE ON events_booking_contracts
        FOR EACH ROW BEGIN IF OLD.candidate IS NOT NULL AND NOT (OLD.candidate <=> NEW.candidate) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Availability candidate is immutable'; END IF; END"""
        )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM events_booking_contracts WHERE candidate IS NOT NULL"))
        .scalar()
    ):
        raise RuntimeError("Archive availability evidence before deliberate downgrade")
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER immutable_candidate ON events_booking_contracts")
    elif dialect == "mysql":
        op.execute("DROP TRIGGER immutable_candidate")
    op.drop_table("events_booking_availability")
    op.drop_column("events_booking_contracts", "candidate")
    if dialect == "postgresql":
        op.execute("DROP FUNCTION events_immutable_candidate()")
        op.execute("DROP FUNCTION events_immutable_availability()")
