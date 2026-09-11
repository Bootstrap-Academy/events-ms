"""Owned migrated schema/clock guard probe, PostgreSQL or MySQL only."""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from api.database import db, db_context
from api.models import BookingContract, BookingPayment, Webinar, WebinarParticipant
from api.services import booking_availability
from api.utils.utc import utcnow


async def main() -> None:
    assert any(s in os.environ["DATABASE_URL"] for s in ["127.0.0.1:55572/l1fixevents", "127.0.0.1:55573/l1fixevents"])
    oid, wid, uid = str(uuid4()), str(uuid4()), str(uuid4())
    start = utcnow() + timedelta(days=1)
    candidate = {
        "order_id": oid,
        "user_id": uid,
        "offer_hash": "synthetic-migration",
        "paid_coins": 0,
        "scheduled_start": start.isoformat(),
    }
    async with db_context():
        await db.add(
            BookingContract(
                id=oid,
                user_id=uid,
                event_id=wid,
                kind="webinar",
                offer={"hash": "synthetic-migration"},
                state="offered",
                candidate=None,
            )
        )
        await db.add(
            BookingPayment(
                id=oid,
                event_id=wid,
                user_id=uid,
                kind="webinar",
                state="free",
                quoted_coins=0,
                paid_coins=0,
                payout_coins=0,
                payout_ratio="0.7",
                description="Synthetic migration probe",
                original={},
            )
        )
        await db.add(
            Webinar(
                id=wid,
                skill_id="synthetic",
                creator=str(uuid4()),
                creation_date=utcnow(),
                name="Synthetic migration probe",
                description="Local fixture only",
                link="local",
                admin_link="local",
                start=start,
                end=start + timedelta(hours=1),
                max_participants=4,
                price=0,
                participants=[],
            )
        )
        await db.add(WebinarParticipant(webinar_id=wid, user_id=uid, paid_coins=0, payment_id=oid))
    async with db.engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT candidate IS NULL FROM events_booking_contracts WHERE id=:id"), {"id": oid})
        ).scalar()
    async with db_context():
        row = await db.get(BookingContract, id=oid)
        assert row is not None
        row.candidate = candidate
        row.state = "candidate"
    ready, actual, proof, observed = await booking_availability.read(oid)
    assert ready and actual == candidate and proof is None and observed is not None and observed < start
    witness = await booking_availability.observe(oid)
    assert witness and witness["candidate_hash"] == booking_availability.digest(candidate)
    async with db_context():
        row = await db.get(BookingContract, id=oid)
        assert row is not None
        row.closed = True
        row.state = "review"
    assert not (await booking_availability.read(oid))[0]
    for statement in [
        "UPDATE events_booking_contracts SET candidate='{}' WHERE id=:id",
        "UPDATE events_booking_availability SET proof=proof WHERE order_id=:id",
        "DELETE FROM events_booking_availability WHERE order_id=:id",
    ]:
        try:
            async with db.engine.begin() as conn:
                await conn.execute(text(statement), {"id": oid})
        except DBAPIError:
            pass
        else:
            raise AssertionError("Immutable proof mutation succeeded: " + statement)
    print(
        "PASS migrated",
        db.engine.dialect.name,
        "SQL-null first candidate, committed source DB clock/read/witness, closure without history loss and immutable candidate/proof guards",
        flush=True,
    )
    await db.engine.dispose()


asyncio.run(main())
