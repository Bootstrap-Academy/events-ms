"""L1 source/financial interruption tests against owned PostgreSQL/backend only."""

import asyncio
import os
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from api.database import Base, db, db_context, filter_by
from api.endpoints.coachings import book_coaching
from api.models import BookingPayment, Coaching, EmergencyCancel, SettlementBatch, SettlementClaim, Slot
from api.models.booking_contract import BookingContract
from api.schemas.user import User, UserInfo
from api.services import booking_contracts, booking_payments, payment_claims
from api.services.internal import InternalService
from api.utils.jwt import encode_jwt
from api.utils.utc import utcnow


FOO = "a8d95e0f-71ae-4c49-995e-695b7c93848c"
HOST = "94d0e3ca-bf16-486b-a172-b87f4bcbd039"


@pytest.fixture
async def ledger(mocker: Any) -> Any:
    if not os.getenv("T6_BACKEND_URL"):
        pytest.skip("requires isolated synthetic backend/PostgreSQL")
    engine = create_async_engine(os.environ["T6_SOURCE_DB"])
    mocker.patch.object(db, "engine", engine)
    async with engine.begin() as schema_conn:
        await schema_conn.run_sync(Base.metadata.drop_all)
        await schema_conn.run_sync(Base.metadata.create_all)
    mocker.patch.object(
        InternalService,
        "client",
        new_callable=property,
        fget=lambda _: AsyncClient(
            base_url=os.environ["T6_BACKEND_URL"],
            headers={
                "Authorization": encode_jwt({"aud": "shop"}, timedelta(minutes=10), secret="synthetic-T6-local-test")
            },
        ),
    )
    info = UserInfo(id=HOST, name="synthetic", display_name="Synthetic host", avatar_url=None)
    mocker.patch("api.services.booking_contracts.get_userinfo", AsyncMock(return_value=info))
    mocker.patch("api.endpoints.coachings.get_userinfo", AsyncMock(return_value=info))
    mocker.patch("api.endpoints.coachings.clear_cache", AsyncMock())
    mocker.patch("api.models.LecturerRating.get_rating", AsyncMock(return_value=None))
    conn = await asyncpg.connect(os.environ["T6_BACKEND_DB"])
    await conn.execute(
        "INSERT INTO coins(user_id,coins,withheld_coins) VALUES($1,100000,0) "
        "ON CONFLICT(user_id) DO UPDATE SET coins=100000",
        UUID(FOO),
    )
    yield conn
    await conn.close()
    await engine.dispose()


async def setup(emergency: bool = False) -> list[str]:
    ids = [str(uuid4()), str(uuid4())]
    async with db_context():
        await db.add(Coaching(user_id=HOST, skill_id="synthetic", price=150))
        for i, sid in enumerate(ids):
            await db.add(
                Slot(
                    id=sid,
                    user_id=HOST,
                    start=utcnow() + timedelta(days=3 + i),
                    end=utcnow() + timedelta(days=3 + i, hours=1),
                    link="synthetic-secret-join",
                )
            )
        if emergency:
            await EmergencyCancel.create(HOST)
    return ids


async def quote(sid: str) -> Any:
    async with db_context():
        slot = await db.get(Slot, id=sid)
        assert slot
        return await booking_contracts.offer(FOO, "coaching", slot, "synthetic")


def accept(q: Any) -> booking_contracts.Acceptance:
    return booking_contracts.Acceptance(
        order_id=q["offer"]["id"], offer_hash=q["offer"]["hash"], accepted=True, early_performance_requested=True
    )


async def book(sid: str, q: Any) -> Any:
    async with db_context():
        return await book_coaching(accept(q), "synthetic", sid, User(id=FOO, email_verified=True, admin=False))


async def test_one_emergency_waiver_two_distinct_bookings(ledger: Any) -> None:
    ids = await setup(True)
    quotes = [await quote(i) for i in ids]
    assert all(q["offer"]["product"]["coins"] == 0 for q in quotes)
    results = await asyncio.gather(*(book(i, q) for i, q in zip(ids, quotes)), return_exceptions=True)
    assert sum(isinstance(r, HTTPException) and r.status_code == 409 for r in results) == 1, results
    async with db_context():
        payments = await db.all(filter_by(BookingPayment))
        assert len(payments) == 1
        p = payments[0]
        assert p.quoted_coins == p.paid_coins == p.payout_coins == 0
        assert p.evidence["operation_id"] is None
        contract = await db.get(BookingContract, id=p.id)
        assert contract and contract.state == "ready"
    assert (
        await ledger.fetchval(
            "SELECT count(*) FROM transactions WHERE id=ANY($1::uuid[])", [UUID(q["offer"]["id"]) for q in quotes]
        )
        == 0
    )


async def test_closed_booking_recovers_original_debit_into_unknown_claim(ledger: Any, mocker: Any) -> None:
    sid = (await setup())[0]
    q = await quote(sid)
    a = accept(q)
    real = AsyncClient.post

    async def lost(self: Any, url: Any, *args: Any, **kwargs: Any) -> Any:
        response = await real(self, url, *args, **kwargs)
        if str(url).startswith("/purchases/events/"):
            raise OSError("remote success lost before source financial commit")
        return response

    mocker.patch.object(AsyncClient, "post", lost)
    with pytest.raises(HTTPException) as e:
        await book(sid, q)
    assert e.value.status_code == 503
    mocker.patch.object(AsyncClient, "post", real)
    async with db_context():
        p = await db.get(BookingPayment, id=str(a.order_id))
        assert p and p.paid_coins is None
        slot = await db.get(Slot, id=sid)
        assert slot
        await db.delete(slot)
        c = await db.get(BookingContract, id=p.id)
        assert c
        c.closed = True
        c.state = "review"
        batch = await db.add(SettlementBatch(id=str(uuid4()), kind="cancellation", event_id=sid, actor_id=HOST))
        await db.session.flush()
        result = await payment_claims.credit(batch.id, sid, FOO, [p], "Synthetic preserved cancellation claim", False)
        assert isinstance(result, dict)
        claim_id = result["payment_claim"]
    async with db_context():
        await booking_payments.deliver(str(a.order_id))
    async with db_context():
        await payment_claims.resolve_pending(None)
        p = await db.get(BookingPayment, id=str(a.order_id))
        assert p and p.paid_coins == 150
        claim = await db.get(SettlementClaim, id=claim_id)
        assert claim and claim.coins == 150
        assert await db.get(Slot, id=sid) is None
        c = await db.get(BookingContract, id=p.id)
        assert c and c.closed and c.state == "review"
    assert await ledger.fetchval("SELECT count(*) FROM transactions WHERE id=$1", a.order_id) == 1


async def test_free_reservation_recovers_without_endpoint_response(ledger: Any, mocker: Any) -> None:
    sid = (await setup(True))[0]
    q = await quote(sid)
    a = accept(q)
    original = booking_payments.deliver

    async def interrupt(_: str) -> str:
        raise OSError("crash after source reservation commit")

    mocker.patch.object(booking_payments, "deliver", interrupt)
    with pytest.raises(OSError):
        await book(sid, q)
    mocker.patch.object(booking_payments, "deliver", original)
    async with db_context():
        p = await db.get(BookingPayment, id=str(a.order_id))
        assert p and p.state == "free"
        assert not await booking_contracts.ready(p)
    await booking_contracts.recover()
    async with db_context():
        p = await db.get(BookingPayment, id=str(a.order_id))
        assert p and await booking_contracts.ready(p)
        c = await db.get(BookingContract, id=p.id)
        assert c and c.reported
    assert await ledger.fetchval("SELECT count(*) FROM transactions WHERE id=$1", a.order_id) == 0


@pytest.mark.parametrize("boundary", ["candidate", "witness", "missing_witness", "missing_link"])
@pytest.mark.parametrize("free", [False, True])
async def test_committed_availability_deadline(ledger: Any, mocker: Any, boundary: str, free: bool) -> None:
    """A delayed witness must not be the first enabling commit; late candidates never enable access."""
    from sqlalchemy import text

    from api.endpoints.calendar import get_events
    from api.endpoints.webinars import get_webinar_by_id, register_for_webinar
    from api.models import CoinOperation, Webinar
    from api.models.webinars import clean_old_webinars
    from api.services import booking_availability
    from api.services.user_export import export_user_data

    mocker.patch("api.endpoints.webinars.clear_cache", AsyncMock())
    info = UserInfo(id=HOST, name="synthetic", display_name="Synthetic host", avatar_url=None)
    mocker.patch("api.models.webinars.get_userinfo", AsyncMock(return_value=info))
    mocker.patch("api.endpoints.calendar.get_userinfo", AsyncMock(return_value=info))
    mocker.patch("api.models.webinars.LecturerRating.create", AsyncMock())
    sid = str(uuid4())
    start = utcnow() + timedelta(seconds=4)
    async with db_context():
        await db.add(
            Webinar(
                id=sid,
                skill_id="synthetic",
                creator=HOST,
                creation_date=utcnow(),
                name="Deadline fixture",
                description="Synthetic",
                admin_link="local-admin",
                link="" if boundary == "missing_link" else "synthetic-secret-join",
                start=start,
                end=start + timedelta(hours=1),
                max_participants=4,
                price=0 if free else 150,
                participants=[],
            )
        )
    async with db_context():
        q = await booking_contracts.offer(FOO, "webinar", await db.get(Webinar, id=sid))
    oid = q["offer"]["id"]

    async def book_webinar() -> Any:
        async with db_context():
            webinar = await db.get(Webinar, id=sid)
            assert webinar is not None
            return await register_for_webinar(accept(q), webinar, User(id=FOO, email_verified=True, admin=False))

    async def participant_view() -> Any:
        webinar = await db.get(Webinar, id=sid)
        assert webinar is not None
        return await get_webinar_by_id(webinar, User(id=FOO, email_verified=True, admin=False))

    if boundary in ("candidate", "witness"):
        table = "events_booking_contracts" if boundary == "candidate" else "events_booking_availability"
        operation = "UPDATE" if boundary == "candidate" else "INSERT"
        condition = "NEW.candidate IS NOT NULL AND OLD.candidate IS NULL" if boundary == "candidate" else "TRUE"
        async with db.engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE FUNCTION l1_delay_commit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    f"BEGIN IF {condition} THEN PERFORM pg_sleep(6); END IF; RETURN NEW; END $$"
                )
            )
            await conn.execute(
                text(
                    f"CREATE CONSTRAINT TRIGGER l1_delay_commit AFTER {operation} ON {table} "
                    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION l1_delay_commit()"
                )
            )
    elif boundary == "missing_witness":
        mocker.patch.object(
            booking_availability,
            "observe",
            AsyncMock(side_effect=OSError("synthetic crash before witness persistence")),
        )

    task = asyncio.create_task(book_webinar())
    try:
        if boundary in ("witness", "missing_witness"):
            # An actual participant consumer obtains the link while the later
            # witness COMMIT is still in flight (or was lost entirely).
            while utcnow() < start:
                async with db_context():
                    view = await participant_view()
                if view and view.link:
                    break
                await asyncio.sleep(0.025)
            assert view and view.link == "synthetic-secret-join" and utcnow() < start
            if boundary == "witness":
                assert not task.done(), "The positive control must overlap the delayed witness COMMIT"
        while utcnow() < start + timedelta(milliseconds=100):
            await asyncio.sleep(0.025)
        if boundary == "candidate":
            assert not task.done()
            async with db_context():
                assert (await participant_view()).link is None
        await task
        if boundary == "missing_link":
            async with db_context():
                webinar = await db.get(Webinar, id=sid)
                assert webinar is not None
                webinar.link = "synthetic-secret-join"  # too late to supply access before start
        await booking_contracts.recover()
        async with db_context():
            payment = await db.get(BookingPayment, id=oid)
            contract = await db.get(BookingContract, id=oid)
            slot = await db.get(Webinar, id=sid)
            assert payment and contract and slot
            expected = boundary == "witness"
            assert await booking_contracts.ready(payment) is expected
            assert bool((await participant_view()).link) is expected
            events = await get_events(FOO, False, *([None] * 13))
            assert bool(next(e for e in events if e.id == sid).link) is expected
            exported = await export_user_data(FOO)
            retained = next(c for c in exported.purchase_contracts if c["id"] == oid)
            assert (retained["availability_observation"] is not None) is expected
            assert payment.paid_coins == (0 if free else 150)
            if expected:
                assert contract.reported and contract.fulfillment
                assert contract.candidate is not None
                assert booking_availability.instant(contract.fulfillment["provided_at"]) < start
                assert contract.fulfillment["candidate_hash"] == booking_availability.digest(contract.candidate)
            else:
                assert contract.state == "review" and contract.fulfillment is None
            slot.end = utcnow() - timedelta(seconds=1)  # synthetic cleanup eligibility
        await clean_old_webinars()
        async with db_context():
            operations = await db.all(filter_by(CoinOperation))
            assert [op.coins for op in operations] == ([105] if expected and not free else [])
        assert await ledger.fetchval("SELECT count(*) FROM transactions WHERE id=$1", UUID(oid)) == (0 if free else 1)
        # Exercise original-order recovery immediately instead of waiting for
        # the independent background worker's interval.
        async with InternalService.SHOP.client as client:
            response = await client.post(f"/purchases/events/{FOO}", json=accept(q).payload())
        assert response.status_code == 200, response.text
        outcome = response.json()
        if boundary == "witness":
            # A review already opened before the historical report arrived is
            # retained; timely source proof does not erase an operator's review.
            assert outcome["state"] in ("fulfilled", "review")
            assert outcome["provision_timing"]["committed_before_deadline_proven"] is True
        else:
            assert outcome["state"] == "review" and outcome["fulfillment"] is None
    finally:
        if not task.done():
            await task
        if boundary in ("candidate", "witness"):
            async with db.engine.begin() as conn:
                await conn.execute(text(f"DROP TRIGGER l1_delay_commit ON {table}"))
                await conn.execute(text("DROP FUNCTION l1_delay_commit()"))


async def test_unsupported_sqlite_cannot_issue_prospective_booking(session: Any) -> None:
    # The ordinary unit fixture is SQLite. This is an actual capability guard,
    # before profile lookup, offer issuance, declaration or payment dispatch.
    assert db.engine.dialect.name == "sqlite"
    with pytest.raises(HTTPException) as denied:
        await booking_contracts.offer(FOO, "webinar", object())
    assert denied.value.status_code == 503
