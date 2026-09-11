"""Ordinary SQLite outcomes; separate SQLite snapshots simulate committed discovery.

These sequential tests exercise real ORM flush/rollback, not native lock waits.
Remote original declarations, succession authority and settlement are fixtures.
"""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine

from api.database import Base, db, filter_by, select
from api.models import BookingPayment, EventRightGrant, RetainedEventRight, SettlementClaim, Webinar, WebinarParticipant
from api.models.booking_contract import BookingContract
from api.models.settlement import CoinOperation
from api.models.webinar_participants import own_seat_changes
from api.models.webinars import clean_old_webinars
from api.services import commercial, event_cancellations, retained_events, settlements
from api.services.user_deletion import delete_user_data
from tests.payment_fixtures import paid_participant
from tests.services.test_event_cancellations import declaration
from tests.services.test_event_succession import authorize, preserved
from tests.services import test_retained_events
from tests.services.test_retained_events import booking, canonical
from tests.services.test_user_deletion import OTHER, THIRD, USER


remote = test_retained_events.remote


@pytest.fixture
async def committed_snapshot(monkeypatch):
    """A separate local SQLite view, explicitly selected instead of the SQLite shortcut.

    The real owning session still uses its fixture engine. This is a projection
    boundary control; neither its view nor SQL execution models InnoDB isolation.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def snapshot():
        await db.session.flush()
        async with engine.begin() as connection:
            for model in (BookingPayment, BookingContract, RetainedEventRight, WebinarParticipant):
                rows = (await db.exec(model.__table__.select())).mappings().all()
                if rows:
                    await connection.execute(model.__table__.insert(), [dict(row) for row in rows])
        monkeypatch.setattr(db, "committed_read_engine", engine)
        monkeypatch.setattr(db, "engine", SimpleNamespace(dialect=SimpleNamespace(name="committed_projection_test")))
        return engine

    yield snapshot
    await engine.dispose()


async def ended_host(remote, mocker):
    event, right, payment_id = await preserved(remote, role="instructor")
    grant_id, _, _ = await authorize(mocker, right)
    await retained_events.deliver(USER, grant_id)
    await db.commit()
    mocker.patch("api.models.webinars.utcnow", return_value=event.end + timedelta(seconds=1))
    await clean_old_webinars.__wrapped__()
    await db.commit()
    assert await db.get(Webinar, id=event.id) is None
    claims = await db.all(select(SettlementClaim))
    assert len(claims) == 1 and claims[0].user_id == USER
    assert await retained_events.right_for(payment_id, "participant") is None
    return event, right, await db.get(BookingPayment, id=payment_id), grant_id


@pytest.mark.parametrize("payer_first", [True, False])
async def test_detached_original_payer_and_provider_keep_one_component_in_either_order(
    session, remote, mocker, payer_first
):
    event, right, payment, grant_id = await ended_host(remote, mocker)
    payment_before = deepcopy((payment.user_id, payment.original, payment.evidence))
    command, receipt = declaration(mocker, right, event.start - timedelta(microseconds=1))
    remote[OTHER] = canonical(OTHER)
    if payer_first:
        await delete_user_data(OTHER)
        first = await db.first(filter_by(SettlementClaim, user_id=OTHER))
        assert first.entitlement == "pending_evidence"
        identity = deepcopy((first.id, first.created_at, first.basis))
    result = await event_cancellations.receive(USER, command)
    if not payer_first:
        first = await db.first(filter_by(SettlementClaim, user_id=OTHER))
        identity = deepcopy((first.id, first.created_at, first.basis))
        await delete_user_data(OTHER)
    await delete_user_data(OTHER)
    assert await event_cancellations.receive(USER, command) == result
    claims = await db.all(filter_by(SettlementClaim, user_id=OTHER))
    assert len(claims) == 1
    claim = claims[0]
    assert (claim.id, claim.created_at, claim.basis) == identity
    assert claim.payment_ids == [payment.id] and claim.entitlement == "established"
    operations = await db.all(filter_by(CoinOperation, user_id=OTHER))
    assert len(operations) == 1 and operations[0].id == claim.id and operations[0].coins == payment.paid_coins
    assert (payment.user_id, payment.original, payment.evidence) == payment_before
    assert (await db.get(EventRightGrant, id=grant_id)).state == "withdrawn"
    assert result["received_at"] == receipt["received_at"]
    evidence = await event_cancellations.claim_evidence(claim.id)
    assert len(evidence) == 1 and evidence[0]["command_id"] == command
    assert await db.get(Webinar, id=event.id) is None


async def test_detached_claim_flush_rollback_then_exact_batch_retry(session, remote, mocker):
    _, _, payment, _ = await ended_host(remote, mocker)
    remote[OTHER] = canonical(OTHER)
    receipt = await commercial.erasure_receipt(OTHER)
    batch = await settlements.new_batch("deletion", OTHER)
    await db.commit()
    batch_id, payment_id = batch.id, payment.id
    original_receipt = deepcopy(receipt.canonical)
    await commercial.preserve_detached_claims(OTHER, receipt, batch_id)
    await session.flush()
    assert len(await db.all(filter_by(SettlementClaim, user_id=OTHER))) == 1
    await session.rollback()
    assert await db.all(filter_by(SettlementClaim, user_id=OTHER)) == []
    assert await db.all(filter_by(CoinOperation, user_id=OTHER)) == []
    receipt = await db.get(commercial.CommercialErasureReceipt, subject=OTHER)
    await commercial.preserve_detached_claims(OTHER, receipt, batch_id)
    await db.commit()
    first = await db.first(filter_by(SettlementClaim, user_id=OTHER))
    identity = first.id
    await commercial.preserve_detached_claims(OTHER, receipt, batch_id)
    await db.commit()
    assert (await db.first(filter_by(SettlementClaim, user_id=OTHER))).id == identity
    assert first.payment_ids == [payment_id] and first.entitlement == "pending_evidence"
    assert receipt.canonical == original_receipt
    assert len(await db.all(filter_by(CoinOperation, user_id=OTHER))) == 1


async def test_detached_discovers_committed_and_own_payments_before_withdrawing_grants(
    session, remote, mocker, committed_snapshot
):
    _, _, payment, _ = await ended_host(remote, mocker)
    committed_id = payment.id
    await db.exec(
        update(BookingPayment)
        .where(BookingPayment.id == committed_id)
        .values(paid_coins=None)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    assert payment.paid_coins is not None  # loaded amount is deliberately stale
    await committed_snapshot()
    own = await db.add(
        BookingPayment(
            id=str(uuid4()),
            event_id=str(uuid4()),
            user_id=OTHER,
            kind="webinar",
            state="legacy_unknown",
            quoted_coins=None,
            paid_coins=None,
            payout_coins=None,
            payout_ratio=None,
            description="Unresolved original booking",
            original={"unmodified_source": "local_observation"},
            evidence=None,
        )
    )
    own_id = own.id
    # Model a prior main-transaction snapshot omitting the committed payment;
    # actual own records remain visible. The reserved reader executes real SQL.
    execute = db.exec

    async def old_payment_projection(query):
        if (
            getattr(query, "_for_update_arg", None) is None
            and hasattr(query, "selected_columns")
            and list(query.selected_columns.keys()) == ["id"]
            and query.column_descriptions[0]["entity"] is BookingPayment
        ):
            query = query.where(BookingPayment.id != committed_id)
        return await execute(query)

    mocker.patch.object(db, "exec", side_effect=old_payment_projection)
    lock_order = []
    first = db.first

    async def observe_lock(query):
        if (
            getattr(query, "_for_update_arg", None) is not None
            and query.column_descriptions[0]["entity"] is BookingPayment
        ):
            row = await first(query)
            lock_order.append(row.id)
            return row
        return await first(query)

    mocker.patch.object(db, "first", side_effect=observe_lock)
    preserved_right = retained_events.preserved

    async def verify_inventory_before_claims(payment_id, subject=None):
        assert {committed_id, own_id} <= set(lock_order)
        return await preserved_right(payment_id, subject)

    mocker.patch.object(retained_events, "preserved", side_effect=verify_inventory_before_claims)
    withdraw = retained_events.withdraw_grants

    async def verify_payment_ownership(subject):
        assert {committed_id, own_id} <= set(lock_order)
        await withdraw(subject)

    mocker.patch.object(retained_events, "withdraw_grants", side_effect=verify_payment_ownership)
    remote[OTHER] = canonical(OTHER)
    await delete_user_data(OTHER)
    claims = await db.all(filter_by(SettlementClaim, user_id=OTHER))
    assert {tuple(claim.payment_ids) for claim in claims} == {(committed_id,), (own_id,)}
    assert all(claim.coins is None for claim in claims)
    assert payment.paid_coins is None
    unknown = next(claim for claim in claims if claim.payment_ids == [own_id])
    assert unknown.coins is None and unknown.entitlement == "pending_evidence"
    assert await db.get(CoinOperation, id=unknown.id) is None
    assert own.user_id == OTHER and own.original == {"unmodified_source": "local_observation"}


@pytest.mark.parametrize(
    "state,recipient,expected", [("cancelled", THIRD, False), ("active", OTHER, False), ("active", THIRD, True)]
)
async def test_preserved_refreshes_mutable_ownership_and_state(session, remote, state, recipient, expected):
    _, right, payment_id = await preserved(remote)
    right.source_subject = OTHER
    right.current_subject = THIRD
    right.state = "active"
    await db.commit()
    await db.exec(
        update(RetainedEventRight)
        .where(RetainedEventRight.id == right.id)
        .values(state=state, current_subject=recipient)
        .execution_options(synchronize_session=False)
    )
    assert right.current_subject == THIRD and right.state == "active"  # deliberately stale ORM object
    assert await retained_events.preserved(payment_id, THIRD) is expected
    assert right.current_subject == recipient and right.state == state


async def test_right_creation_own_flush_and_rollback_preserve_original_identity(session, remote, mocker):
    event, booked = await booking()
    payment = await db.get(BookingPayment, id=booked.payment_id)
    receipt = SimpleNamespace(canonical=canonical(USER), observed_at=event.start)
    await db.commit()
    event_id, payment_id = event.id, payment.id
    probes = mocker.spy(db, "first")
    assert await retained_events.preserve_on_erasure(USER, receipt, payment, event, "participant")
    right = await retained_events.right_for(payment_id, "participant")
    original_id, original_bytes = right.id, deepcopy(right.original)
    assert await retained_events.preserve_on_erasure(USER, receipt, payment, event, "participant")
    assert (await retained_events.right_for(payment_id, "participant")).original == original_bytes
    for call in probes.call_args_list:
        query = call.args[0]
        if getattr(query, "_for_update_arg", None) is not None and "events_retained_rights" in str(query):
            assert "events_retained_rights.id =" in str(query)
    await session.rollback()
    assert await retained_events.right_for(payment_id, "participant") is None
    event = await db.get(Webinar, id=event_id)
    payment = await db.get(BookingPayment, id=payment_id)
    await retained_events.preserve_on_erasure(USER, receipt, payment, event, "participant")
    assert (await retained_events.right_for(payment_id, "participant")).id == original_id
    assert len(await db.all(select(RetainedEventRight))) == 1


@pytest.mark.parametrize("change", ["delete_target", "move_original", "add_target", "delete_original"])
async def test_delivery_applies_actual_own_flushed_occupancy(session, remote, mocker, committed_snapshot, change):
    event, right, payment_id = await preserved(remote)
    target = None
    if change == "delete_target":
        target = await db.add(paid_participant(webinar_id=event.id, user_id=THIRD, paid_coins=99))
        await db.commit()
    await committed_snapshot()
    original = await db.get(WebinarParticipant, webinar_id=event.id, user_id=USER)
    if change == "delete_target":
        await db.delete(target)
    elif change == "move_original":
        original.user_id = THIRD
    elif change == "add_target":
        await db.add(paid_participant(webinar_id=event.id, user_id=THIRD, paid_coins=99))
    else:
        await db.delete(original)
    await session.flush()
    grant_id, _, _ = await authorize(mocker, right)
    if change in {"add_target", "delete_original"}:
        with pytest.raises(HTTPException) as denied:
            await retained_events.deliver(USER, grant_id)
        assert denied.value.status_code == 409 and await db.all(select(EventRightGrant)) == []
    else:
        first = await retained_events.deliver(USER, grant_id)
        await db.commit()
        assert first["state"] == "granted" and await retained_events.deliver(USER, grant_id) == first
        seats = await db.all(filter_by(WebinarParticipant, webinar_id=event.id))
        assert [(row.user_id, row.payment_id) for row in seats] == [(THIRD, payment_id)]
    assert (await db.get(BookingPayment, id=payment_id)).user_id == USER


@pytest.mark.parametrize("commit_inner", [True, False])
async def test_seat_journal_tracks_autoflush_nested_commit_rollback_and_outer_rollback(
    session, remote, committed_snapshot, commit_inner
):
    event, right, payment_id = await preserved(remote)
    event_id = event.id
    await committed_snapshot()
    booked = await db.get(WebinarParticipant, webinar_id=event_id, user_id=USER)
    outer = await session.begin_nested()
    booked.user_id = THIRD
    # This service query autoflushes through its normal flush boundary.
    assert (await retained_events.webinar_seats(event_id, payment_id, THIRD))[0].user_id == THIRD
    inner = await session.begin_nested()
    await db.delete(booked)
    assert (await retained_events.webinar_seats(event_id, payment_id, THIRD))[0] is None
    if commit_inner:
        await inner.commit()
        assert (await retained_events.webinar_seats(event_id, payment_id, THIRD))[0] is None
    else:
        await inner.rollback()
        assert (await retained_events.webinar_seats(event_id, payment_id, THIRD))[0].user_id == THIRD
    await outer.rollback()
    assert (await retained_events.webinar_seats(event_id, payment_id, THIRD))[0].user_id == USER
    assert own_seat_changes(session.sync_session) == {}
    booked = (await retained_events.webinar_seats(event_id, payment_id, THIRD))[0]
    booked.user_id = THIRD
    await session.flush()
    assert own_seat_changes(session.sync_session)
    await session.rollback()
    assert own_seat_changes(session.sync_session) == {}
    assert (await retained_events.webinar_seats(event_id, payment_id, THIRD))[0].user_id == USER


async def test_seat_journal_cascade_and_outer_commit_release_changes(session, remote, committed_snapshot):
    event, _, payment_id = await preserved(remote)
    event_id = event.id
    await committed_snapshot()
    await db.delete(event)
    await session.flush()
    assert (await retained_events.webinar_seats(event_id, payment_id, THIRD))[0] is None
    assert own_seat_changes(session.sync_session)[(event_id, USER)] is None
    await db.commit()
    assert own_seat_changes(session.sync_session) == {}


@pytest.mark.parametrize("no_op_dirty", [False, True])
async def test_seat_discovery_ignores_a_stale_loaded_destination(
    session, remote, mocker, committed_snapshot, no_op_dirty
):
    event, right, payment_id = await preserved(remote)
    target = await db.add(paid_participant(webinar_id=event.id, user_id=THIRD, paid_coins=99))
    await db.commit()
    target_key = (event.id, THIRD)
    # Model an already committed deletion outside this owning ORM identity map.
    await db.exec(WebinarParticipant.__table__.delete().where(WebinarParticipant.user_id == THIRD))
    await db.commit()
    assert target.user_id == THIRD
    if no_op_dirty:
        target.paid_coins = target.paid_coins
        assert target in session.dirty and not session.is_modified(target)
    await committed_snapshot()
    probes = mocker.spy(db, "first")
    booked, other = await retained_events.webinar_seats(event.id, payment_id, THIRD)
    assert booked.user_id == USER and other is None
    seat_locks = [
        call.args[0] for call in probes.call_args_list if getattr(call.args[0], "_for_update_arg", None) is not None
    ]
    assert len(seat_locks) == 1
    assert target_key[1] not in seat_locks[0].compile().params.values()
