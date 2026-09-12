"""T9: real PostgreSQL reservations, keyed backend debits, claims and reconciliation."""

import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pytest_mock import MockerFixture

from api.database import db, db_context, filter_by, select
from api.endpoints.calendar import cancel_event
from api.endpoints.coachings import book_coaching
from api.endpoints.webinars import register_for_webinar
from api.exceptions.coaching import NotEnoughCoinsError
from api.models import (
    BookingPayment,
    Coaching,
    CoinOperation,
    EmergencyCancel,
    EventType,
    SettlementClaim,
    Slot,
    Webinar,
    WebinarParticipant,
)
from api.models.slots import clean_old_slots
from api.models.webinars import clean_old_webinars
from api.reconcile_payments import report, resolve
from api.schemas.user import User, UserInfo
from api.services import booking_contracts, booking_payments
from api.services.settlements import recover_settlements
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data
from api.utils.utc import utcnow
from tests.services.test_settlements import BAR as HOST
from tests.services.test_settlements import FOO


pytest_plugins = ["tests.services.test_settlements"]


@pytest.fixture
async def booking_ledger(ledger: Any, mocker: MockerFixture) -> Any:
    mocker.patch("api.endpoints.webinars.clear_cache", AsyncMock())
    mocker.patch("api.endpoints.coachings.clear_cache", AsyncMock())
    info = UserInfo(id=HOST, name="host", display_name="Synthetic host", avatar_url=None)
    for path in [
        "api.models.webinars.get_userinfo",
        "api.services.booking_contracts.get_userinfo",
        "api.endpoints.coachings.get_userinfo",
        "api.endpoints.calendar.get_userinfo",
    ]:
        mocker.patch(path, AsyncMock(return_value=info))
    mocker.patch("api.models.webinars.LecturerRating.get_rating", AsyncMock(return_value=None))
    mocker.patch("api.models.webinars.LecturerRating.create", AsyncMock())
    await ledger.execute(
        "INSERT INTO coins (user_id,coins,withheld_coins) VALUES ($1,10000,0) "
        "ON CONFLICT (user_id) DO UPDATE SET coins=10000,withheld_coins=0",
        UUID(FOO),
    )
    return ledger


async def event(kind: str, price: int = 150, emergency: bool = False) -> None:
    async with db_context():
        if kind == "webinar":
            await db.add(
                Webinar(
                    id="event",
                    skill_id="test",
                    creator=HOST,
                    creation_date=utcnow(),
                    name="T9 event",
                    description="synthetic",
                    admin_link="local",
                    link="local",
                    start=utcnow() + timedelta(days=3),
                    end=utcnow() + timedelta(days=3, hours=1),
                    max_participants=10,
                    price=price,
                    participants=[],
                )
            )
        else:
            await Slot.create(HOST, utcnow() + timedelta(days=3), utcnow() + timedelta(days=3, hours=1))
            slot = await db.first(select(Slot))
            assert slot is not None
            slot.id = "event"
            await db.add(Coaching(user_id=HOST, skill_id="test", price=price))
            await db.add(Coaching(user_id=HOST, skill_id="different", price=9999))
        if emergency:
            await EmergencyCancel.create(HOST)


async def book(kind: str, quote: dict[str, Any] | None = None) -> Any:
    if quote is None:
        async with db_context():
            event: Any
            if kind == "webinar":
                event = await db.get(Webinar, id="event")
            else:
                event = await db.get(Slot, id="event")
            assert event
            quote = await booking_contracts.offer(FOO, kind, event, None if kind == "webinar" else "test")
    data = booking_contracts.Acceptance(
        order_id=quote["offer"]["id"],
        offer_hash=quote["offer"]["hash"],
        accepted=True,
        early_performance_requested=True,
    )
    async with db_context():
        user = User(id=FOO, email_verified=True, admin=False)
        if kind == "webinar":
            webinar = await db.get(Webinar, id="event")
            assert webinar
            return await register_for_webinar(data, webinar, user)
        return await book_coaching(data, "test", "event", user)


async def cancel(actor: str = FOO) -> Any:
    async with db_context():
        return await cancel_event("event", User(id=actor, email_verified=True, admin=False))


@pytest.mark.parametrize("kind", ["webinar", "coaching"])
async def test_lost_debit_cancel_rebook_and_concurrent_recovery(
    booking_ledger: Any, mocker: MockerFixture, kind: str
) -> None:
    await event(kind)
    real_debit = booking_contracts.deliver

    async def lose(*args: Any) -> str:
        assert await real_debit(*args) == "paid"
        raise OSError("lost successful debit response")

    mocker.patch.object(booking_contracts, "deliver", side_effect=lose)
    with pytest.raises(HTTPException) as pending:
        await book(kind)
    assert cast(Any, pending.value.detail)["booking_reserved"] is True
    async with db_context():
        old = await db.first(select(BookingPayment))
        assert old is not None
        old_id = old.id
        assert old.paid_coins is None and old.state == "pending"
        if kind == "webinar":
            webinar = await db.get(Webinar, id="event")
            assert webinar
            webinar.price = 240
        else:
            coaching = await db.get(Coaching, user_id=HOST, skill_id="test")
            assert coaching
            coaching.price = 240
    with pytest.raises(HTTPException) as cancellation:
        await cancel()
    assert cast(Any, cancellation.value.detail)["cancellation_committed"] is True
    async with db_context():
        claims = await db.all(select(SettlementClaim))
        assert len(claims) == 2 and all(claim.coins is None for claim in claims)
        assert await db.all(select(CoinOperation)) == []
        assert len((await export_user_data(FOO)).settlement_claims) == 1
    mocker.patch.object(booking_contracts, "deliver", side_effect=real_debit)
    await book(kind)
    await asyncio.gather(booking_payments.recover_booking_payments(), booking_payments.recover_booking_payments())
    await asyncio.gather(recover_settlements(), recover_settlements())
    async with db_context():
        old = await db.get(BookingPayment, id=old_id)
        assert old and old.paid_coins == 150
        current = await db.first(select(WebinarParticipant if kind == "webinar" else Slot))
        assert current is not None
        assert current.payment_id != old_id
        assert (current.paid_coins if kind == "webinar" else current.student_coins) == 240
        ops = await db.all(select(CoinOperation))
        assert sorted(op.coins for op in ops) == [52, 75]
        assert all(op.completed_at for op in ops)
        assert await booking_ledger.fetchval("SELECT coins FROM transactions WHERE id=$1", UUID(old_id)) == -150
        assert await booking_ledger.fetchval("SELECT count(*) FROM transactions WHERE id=$1", UUID(old_id)) == 1


@pytest.mark.parametrize("kind", ["webinar", "coaching"])
@pytest.mark.parametrize("price,emergency", [(0, False), (150, True), (150, False)])
async def test_actual_charge_free_emergency_and_payout(
    booking_ledger: Any, kind: str, price: int, emergency: bool
) -> None:
    await event(kind, price, emergency)
    await book(kind)
    async with db_context():
        payment = await db.first(select(BookingPayment))
        assert payment is not None
        expected = 0 if emergency else price
        assert payment.paid_coins == expected
        operation = await booking_ledger.fetchval("SELECT coins FROM transactions WHERE id=$1", UUID(payment.id))
        assert operation == (-expected if expected else None)
        assert not await EmergencyCancel.exists(HOST)
        model = Webinar if kind == "webinar" else Slot
        old = await db.first(filter_by(model, id="event"))
        assert old
        old.end = utcnow() - timedelta(hours=1)
    await (clean_old_webinars() if kind == "webinar" else clean_old_slots())
    async with db_context():
        ops = await db.all(select(CoinOperation))
        assert [op.coins for op in ops] == ([105] if expected else [])
        assert await db.get(BookingPayment, id=payment.id) is not None


@pytest.mark.parametrize("kind", ["webinar", "coaching"])
async def test_insufficient_funds_and_ack_commit_failure(booking_ledger: Any, mocker: MockerFixture, kind: str) -> None:
    await event(kind)
    await booking_ledger.execute("UPDATE coins SET coins=0 WHERE user_id=$1", UUID(FOO))
    with pytest.raises(NotEnoughCoinsError):
        await book(kind)
    async with db_context():
        failed = await db.first(select(BookingPayment))
        assert failed is not None
        assert failed.state == "failed" and failed.paid_coins == 0
    await booking_ledger.execute("UPDATE coins SET coins=10000 WHERE user_id=$1", UUID(FOO))
    commit = db.commit
    calls = 0

    async def fail_ack() -> None:
        nonlocal calls
        calls += 1
        if any(
            isinstance(row, BookingPayment) and row.state == "paid"
            for row in cast(Any, db.session.identity_map).values()
        ):
            raise OSError("lost events acknowledgement commit")
        await commit()

    mocker.patch.object(db, "commit", side_effect=fail_ack)
    with pytest.raises(OSError):
        await book(kind)
    mocker.patch.object(db, "commit", side_effect=commit)
    await booking_payments.recover_booking_payments()
    async with db_context():
        payments = await db.all(select(BookingPayment))
        assert len(payments) == 2
        assert sorted(p.state for p in payments) == ["failed", "paid"]
        paid = next(p for p in payments if p.state == "paid")
        assert await booking_ledger.fetchval("SELECT coins FROM transactions WHERE id=$1", UUID(paid.id)) == -150


async def legacy(kind: str) -> str:
    await event(kind, 9999)
    async with db_context():
        payment = await db.add(
            BookingPayment(
                id=str(uuid4()),
                event_id="event",
                user_id=FOO,
                kind=kind,
                state="legacy_unknown",
                description="Historical T9",
                original={"guessed_paid_coins": 9999},
            )
        )
        if kind == "webinar":
            await db.add(WebinarParticipant(webinar_id="event", user_id=FOO, paid_coins=None, payment_id=payment.id))
        else:
            slot = await db.get(Slot, id="event")
            assert slot
            slot.book(FOO, EventType.COACHING, 0, 0, "test")
            slot.payment_id = payment.id
            slot.student_coins = slot.instructor_coins = None
        return payment.id


def proof(tmp_path: Path, payment_id: str, kind: str) -> dict[str, Any]:
    path = tmp_path / "review.txt"
    path.write_text(
        "Synthetic corroborating booking documentation: distinct original registration; not price/name inference"
    )
    return {
        "payment_id": payment_id,
        "event_id": "event",
        "user_id": FOO,
        "kind": kind,
        "booking_link_reviewed": True,
        "reviewed_by": "synthetic reviewer",
        "basis": "Synthetic unique original booking record",
        "document_path": str(path),
        "document_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize("kind", ["webinar", "coaching"])
@pytest.mark.parametrize("free", [False, True])
async def test_legacy_cancellation_claim_resolves_from_actual_evidence(
    booking_ledger: Any, tmp_path: Path, kind: str, free: bool
) -> None:
    payment_id = await legacy(kind)
    with pytest.raises(HTTPException):
        await cancel(HOST)
    async with db_context():
        snapshot = await report()
        assert snapshot["unknown_booking_count"] == 1 and len(snapshot["unresolved_claims"]) == 1
        assert (await export_user_data(FOO)).booking_payments[0].paid_coins is None
        manifest = proof(tmp_path, payment_id, "documented_free_booking" if free else "documented_ledger_debit")
        if not free:
            transaction_id = uuid4()
            await booking_ledger.execute(
                "INSERT INTO transactions (id,user_id,coins,description,created_at,include_in_credit_note) "
                "VALUES ($1,$2,-80,'Old distinct booking',now()-interval '1 day',false)",
                transaction_id,
                UUID(FOO),
            )
            manifest["transaction_id"] = str(transaction_id)
            if kind == "coaching":
                manifest |= {
                    "payout_coins": 56,
                    "payout_document_path": manifest["document_path"],
                    "payout_document_sha256": manifest["document_sha256"],
                }
        await resolve(manifest, booking_ledger)
        payment = await db.get(BookingPayment, id=payment_id)
        assert payment and payment.original == {"guessed_paid_coins": 9999}
        assert payment.paid_coins == (0 if free else 80)
    await recover_settlements()
    await recover_settlements()
    async with db_context():
        ops = await db.all(select(CoinOperation))
        assert [op.coins for op in ops] == ([] if free else [80])
        assert all(op.completed_at for op in ops)


async def test_deletion_with_unknown_debit_preserves_refund(booking_ledger: Any, mocker: MockerFixture) -> None:
    await event("webinar")
    real_debit = booking_contracts.deliver

    async def lose(*args: Any) -> str:
        assert await real_debit(*args) == "paid"
        raise OSError("lost response")

    mocker.patch.object(booking_contracts, "deliver", side_effect=lose)
    with pytest.raises(HTTPException):
        await book("webinar")
    with pytest.raises(HTTPException):
        async with db_context():
            await delete_user_data(HOST)
    mocker.patch.object(booking_contracts, "deliver", side_effect=real_debit)
    await booking_payments.recover_booking_payments()
    await recover_settlements()
    async with db_context():
        assert await db.all(select(Webinar)) == []
        ops = await db.all(select(CoinOperation))
        assert [op.coins for op in ops] == [150]
        assert ops[0].completed_at


@pytest.mark.parametrize("case", ["wrong_user", "credit", "future", "missing", "changed_document", "no_review"])
async def test_legacy_rejects_unreliable_evidence(booking_ledger: Any, tmp_path: Path, case: str) -> None:
    payment_id = await legacy("webinar")
    manifest = proof(tmp_path, payment_id, "documented_ledger_debit")
    transaction_id = uuid4()
    if case != "missing":
        await booking_ledger.execute(
            "INSERT INTO transactions (id,user_id,coins,description,created_at,include_in_credit_note) "
            "VALUES ($1,$2,$3,'Synthetic historical booking',now()+$4::interval,false)",
            transaction_id,
            UUID(HOST if case == "wrong_user" else FOO),
            80 if case == "credit" else -80,
            timedelta(days=1 if case == "future" else -1),
        )
    manifest["transaction_id"] = str(transaction_id)
    if case == "changed_document":
        Path(manifest["document_path"]).write_text("changed")
    if case == "no_review":
        manifest["booking_link_reviewed"] = False
    with pytest.raises(ValueError):
        async with db_context():
            await resolve(manifest, booking_ledger)
    async with db_context():
        payment = await db.get(BookingPayment, id=payment_id)
        assert payment and payment.state == "legacy_unknown" and payment.paid_coins is None


async def test_one_ledger_debit_cannot_fund_two_legacy_rebookings(booking_ledger: Any, tmp_path: Path) -> None:
    from sqlalchemy.exc import IntegrityError

    first = await legacy("webinar")
    second = str(uuid4())
    async with db_context():
        await db.add(
            BookingPayment(
                id=second,
                event_id="event",
                user_id=FOO,
                kind="webinar",
                state="legacy_unknown",
                description="Later registration",
                original={"rebooked": True},
            )
        )
    transaction_id = uuid4()
    await booking_ledger.execute(
        "INSERT INTO transactions (id,user_id,coins,description,created_at,include_in_credit_note) "
        "VALUES ($1,$2,-80,'Synthetic historical debit',now()-interval '1 day',false)",
        transaction_id,
        UUID(FOO),
    )
    manifest = proof(tmp_path, first, "documented_ledger_debit") | {"transaction_id": str(transaction_id)}
    async with db_context():
        await resolve(manifest, booking_ledger)
    with pytest.raises(IntegrityError):
        async with db_context():
            await resolve(manifest | {"payment_id": second}, booking_ledger)
    async with db_context():
        untouched = await db.get(BookingPayment, id=second)
        assert untouched and untouched.paid_coins is None and untouched.evidence is None


@pytest.mark.parametrize("emergency", [False, True])
async def test_reservation_commit_failure_sends_no_debit_or_consumes_waiver(
    booking_ledger: Any, mocker: MockerFixture, emergency: bool
) -> None:
    await event("webinar", emergency=emergency)
    async with db_context():
        webinar = await db.get(Webinar, id="event")
        assert webinar
        quote = await booking_contracts.offer(FOO, "webinar", webinar)
    debit = mocker.patch.object(booking_contracts, "deliver", AsyncMock(return_value="paid"))
    commit = db.commit
    mocker.patch.object(db, "commit", AsyncMock(side_effect=OSError("reservation commit failed")))
    with pytest.raises(OSError):
        await book("webinar", quote)
    mocker.patch.object(db, "commit", side_effect=commit)
    debit.assert_not_awaited()
    async with db_context():
        assert await EmergencyCancel.exists(HOST) is emergency
        assert await db.all(select(BookingPayment)) == []
        assert await db.all(select(WebinarParticipant)) == []


async def test_held_legacy_operation_is_observable_and_never_rewritten(booking_ledger: Any) -> None:
    from api.models import SettlementBatch

    async with db_context():
        batch = await db.add(SettlementBatch(id=str(uuid4()), kind="cancellation", notifications=[]))
        await db.session.flush()
        op = await db.add(
            CoinOperation(
                id=str(uuid4()),
                batch_id=batch.id,
                event_id="old-deleted-event",
                user_id=FOO,
                coins=9999,
                description="Original disputed amount",
                credit_note=False,
                provenance="legacy_hold",
                attempts=2,
                last_error="OldTimeout",
            )
        )
        operation_id = op.id
    await recover_settlements()
    async with db_context():
        original = await db.get(CoinOperation, id=operation_id)
        assert original and original.coins == 9999 and original.attempts == 2 and original.last_error == "OldTimeout"
        assert original.completed_at is None
        assert len((await report())["legacy_operations"]) == 1
    assert (
        await booking_ledger.fetchval("SELECT 1 FROM internal_coin_operations WHERE id=$1", UUID(operation_id)) is None
    )
