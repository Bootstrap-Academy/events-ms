"""Real local settlement/recovery; synthetic remote receipts and SQLite projections.

The separate committed projection controls discovery and ORM refresh, not native
InnoDB waits. Native EC7 ordering has its own independent validation.
"""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator, Sequence, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from _pytest.monkeypatch import MonkeyPatch
from pytest_mock import MockerFixture
from sqlalchemy import false
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm.attributes import set_committed_value

from api.database import Base, db, filter_by, select
from api.models import BookingPayment, EventRightGrant, RetainedEventRight, SettlementClaim, WebinarParticipant
from api.models.booking_payment import CommercialHandoff
from api.models.event_cancellation import EventCancellation, EventCancellationClaimEvidence
from api.models.ordinary_cancellation import OrdinaryCancellationClaimEvidence
from api.models.settlement import CoinOperation, SettlementBatch
from api.services import commercial, event_cancellations, payment_claims, settlements
from api.utils.utc import utcnow
from tests.required import required
from tests.services.test_event_cancellations import declaration, original
from tests.services.test_event_succession import authorize
from tests.services.test_retained_events import booking
from tests.services.test_user_deletion import USER, CommercialResponses


@pytest.fixture(autouse=True)
def commercial_remote(mocker: MockerFixture) -> Iterator[CommercialResponses]:
    remote = CommercialResponses()
    mocker.patch("api.services.shop.commercial", side_effect=remote.__call__)
    mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    legacy = mocker.patch("api.services.shop.apply_coin_operation", AsyncMock())
    yield remote
    assert remote.unexpected == []
    legacy.assert_not_awaited()


@pytest.fixture
def remote(commercial_remote: CommercialResponses) -> dict[str, Any]:
    return commercial_remote.receipts


@pytest.fixture
async def committed_children(
    monkeypatch: MonkeyPatch, mocker: MockerFixture
) -> AsyncIterator[Callable[[Sequence[type[Base]]], Awaitable[None]]]:
    """Actual separate SQLite reader plus an explicitly simulated old local view."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def snapshot(models: Any) -> None:
        await db.session.flush()
        async with engine.begin() as connection:
            for model in models:
                rows = (await db.exec(model.__table__.select())).mappings().all()
                if rows:
                    await connection.execute(model.__table__.insert(), [dict(row) for row in rows])
        monkeypatch.setattr(db, "committed_read_engine", engine)
        monkeypatch.setattr(db, "engine", SimpleNamespace(dialect=SimpleNamespace(name="committed_projection_test")))
        execute, stream = db.exec, db.stream

        def old_view(query: Any) -> Any:
            descriptions = getattr(query, "column_descriptions", [])
            if (
                getattr(query, "_for_update_arg", None) is None
                and descriptions
                and descriptions[0].get("entity") in models
            ):
                return query.where(false())
            return query

        async def old_execute(query: Any) -> Any:
            return await execute(old_view(query))

        async def old_stream(query: Any) -> Any:
            return await stream(old_view(query))

        mocker.patch.object(db, "exec", side_effect=old_execute)
        mocker.patch.object(db, "stream", side_effect=old_stream)

    yield snapshot
    await engine.dispose()


async def claimed(coins: int | None = 1337) -> tuple[SettlementClaim, BookingPayment]:
    event, booked = await booking()
    payment = required(await db.get(BookingPayment, id=booked.payment_id))
    payment.paid_coins = coins
    batch = required(await settlements.new_batch("cancellation", USER, event.id))
    await payment_claims.credit(batch.id, event.id, USER, [payment], "Original component", False)
    await db.commit()
    return required(cast(SettlementClaim | None, await db.first(select(SettlementClaim)))), payment


def immutable_payment(payment: BookingPayment) -> dict[str, Any]:
    return deepcopy({column.name: getattr(payment, column.name) for column in payment.__table__.columns})


async def test_existing_handoff_in_committed_view_replays_without_duplicate_or_receipt_replacement(
    session: AsyncSession,
    commercial_remote: CommercialResponses,
    mocker: MockerFixture,
    committed_children: Callable[[Sequence[type[Base]]], Awaitable[None]],
) -> None:
    claim, payment = await claimed()
    original_payment = immutable_payment(payment)
    assert await commercial.handoff(claim)
    saved = required(await db.get(CommercialHandoff, claim_id=claim.id))
    accepted = deepcopy((saved.payload, saved.receipt, saved.acknowledged_at, saved.attempts))
    operation = required(await db.get(CoinOperation, id=claim.id))
    # Loaded objects are deliberately older than durable bytes. No historical
    # payment/operation/receipt is actually rewritten by this fixture.
    set_committed_value(saved, "payload", {"old_snapshot": True})
    set_committed_value(saved, "acknowledged_at", None)
    set_committed_value(operation, "coins", 2)
    set_committed_value(claim, "entitlement", "pending_evidence")
    await committed_children((CommercialHandoff, CoinOperation))
    locks = mocker.spy(db, "first")
    assert await commercial.handoff(claim)
    assert (saved.payload, saved.receipt, saved.acknowledged_at, saved.attempts) == accepted
    assert len(commercial_remote.bodies("register_event")) == 1
    assert operation.coins == 1337 and operation.completed_at is None
    assert claim.entitlement == "established" and immutable_payment(payment) == original_payment
    queries = [
        call.args[0] for call in locks.call_args_list if getattr(call.args[0], "_for_update_arg", None) is not None
    ]
    assert queries[0].column_descriptions[0]["entity"] is SettlementClaim
    assert all(query.column_descriptions[0]["entity"] is not BookingPayment for query in queries)
    assert all(query.get_execution_options().get("populate_existing") is True for query in queries)


@pytest.mark.parametrize("coins", [None, 0, 1337])
async def test_absent_children_and_own_flushed_handoff_preserve_one_component(
    session: AsyncSession,
    commercial_remote: CommercialResponses,
    mocker: MockerFixture,
    committed_children: Callable[[Sequence[type[Base]]], Awaitable[None]],
    coins: int | None,
) -> None:
    claim, payment = await claimed(coins)
    payment_before = immutable_payment(payment)
    # The committed reader has no child rows; discovery must still include
    # this owning session's subsequently flushed handoff.
    await committed_children(())
    probes = []
    first = db.first

    async def existing_only(query: Any) -> Any:
        if getattr(query, "_for_update_arg", None) is not None:
            model = query.column_descriptions[0]["entity"]
            if model in {CommercialHandoff, CoinOperation}:
                # Checking actual pre-query existence rejects any absent-key
                # locking probe, including one hidden by SQLite's ignored FOR UPDATE.
                plain = filter_by(model, **{"claim_id" if model is CommercialHandoff else "id": claim.id})
                assert await first(plain) is not None
                probes.append(model)
        return await first(query)

    mocker.patch.object(db, "first", side_effect=existing_only)
    assert await commercial.handoff(claim)
    saved = required(await db.get(CommercialHandoff, claim_id=claim.id))
    payload = deepcopy(saved.payload)
    assert payload["observation"]["computed_units"] == coins
    assert ("operation_id" in payload) is bool(coins)
    assert len(await db.all(select(CommercialHandoff))) == 1
    # Own replacement is flushed but not committed before discovery.
    await db.delete(saved)
    await session.flush()
    own = await db.add(CommercialHandoff(claim_id=claim.id, payload=payload))
    await session.flush()
    assert await commercial.handoff(claim)
    assert (await db.get(CommercialHandoff, claim_id=claim.id)) is own
    assert own.payload == payload and own.acknowledged_at is not None
    assert len(await db.all(select(CommercialHandoff))) == 1
    assert immutable_payment(payment) == payment_before
    assert CommercialHandoff in probes


@pytest.mark.parametrize("model", [CommercialHandoff, CoinOperation])
async def test_confirmed_child_disappearance_fails_without_remote_or_replacement(
    session: AsyncSession, mocker: MockerFixture, commercial_remote: CommercialResponses, model: Any
) -> None:
    claim, _ = await claimed()
    discover = payment_claims.committed_and_local_keys

    async def confirmed(query: Any) -> Any:
        if query.column_descriptions[0]["entity"] is model:
            return {(claim.id,)}
        return await discover(query)

    first = db.first

    async def disappeared(query: Any) -> Any:
        if getattr(query, "_for_update_arg", None) is not None and query.column_descriptions[0]["entity"] is model:
            return None
        return await first(query)

    mocker.patch.object(payment_claims, "committed_and_local_keys", side_effect=confirmed)
    mocker.patch.object(db, "first", side_effect=disappeared)
    with pytest.raises(ValueError, match="disappeared"):
        await commercial.handoff(claim)
    assert commercial_remote.bodies("register_event") == []
    assert await db.all(select(CommercialHandoff)) == []


@pytest.mark.parametrize(
    "model,origin",
    [
        (EventCancellationClaimEvidence, "retained_claimant"),
        (OrdinaryCancellationClaimEvidence, "ordinary_authenticated"),
    ],
)
async def test_committed_cancellation_evidence_is_exported_without_rewriting_claim(
    session: AsyncSession,
    mocker: MockerFixture,
    commercial_remote: CommercialResponses,
    committed_children: Callable[[Sequence[type[Base]]], Awaitable[None]],
    model: Any,
    origin: str,
) -> None:
    claim, payment = await claimed()
    identity = deepcopy((claim.id, claim.payment_ids, claim.basis, claim.created_at, claim.coins))
    assessment = {"payment_ids": [payment.id], "entitlement": "established", "basis": {"original": "assessment"}}
    # Synthetic evidence row for the read boundary; actual declaration producers
    # are covered by the cancellation regressions and the native schedule.
    row = await db.add(model(command_id=str(uuid4()), claim_id=claim.id, observed_at=utcnow(), assessment=assessment))
    await db.commit()
    set_committed_value(row, "assessment", {"old_snapshot": True})
    await committed_children((model,))
    assert await commercial.handoff(claim)
    evidence = commercial_remote.bodies("register_event")[0]["observation"]["cancellation_evidence"]
    assert len(evidence) == 1
    assert evidence[0]["origin"] == origin and evidence[0]["command_id"] == row.command_id
    assert evidence[0]["assessment"] == assessment
    assert (claim.id, claim.payment_ids, claim.basis, claim.created_at, claim.coins) == identity


@pytest.mark.parametrize("failure", ["lost", "identity", "disposition"])
async def test_committed_cancellation_returns_original_result_after_real_finish_failure_and_exact_recovery(
    session: AsyncSession,
    remote: dict[str, Any],
    commercial_remote: CommercialResponses,
    mocker: MockerFixture,
    failure: str,
) -> None:
    event, right, payment = await original(remote)
    grant_id, _, _ = await authorize(mocker, right)
    from api.services import retained_events

    await retained_events.deliver(USER, grant_id)
    await db.commit()
    command, receipt = declaration(mocker, right)
    payment_id, right_id, event_id = payment.id, right.id, event.id
    payment_before = immutable_payment(payment)
    commercial_remote.handoff_failure = failure
    finish = mocker.spy(settlements, "finish")
    rollback = mocker.spy(session, "rollback")
    result = await event_cancellations.receive(USER, command)
    assert finish.await_count == 1 and rollback.await_count == 1
    assert result["state"] == "applied" and result["financial_satisfaction"] is False
    assert result["payment_id"] == payment_id and result["received_at"] == receipt["received_at"]
    saved = required(await db.get(EventCancellation, id=command))
    assert saved.original == receipt and saved.result == result
    assert await db.get(SettlementBatch, id=result["settlement_batch_id"]) is not None
    assert (required(await db.get(RetainedEventRight, id=right_id))).state == "cancelled"
    assert (required(await db.get(EventRightGrant, id=grant_id))).state == "withdrawn"
    assert await db.all(filter_by(WebinarParticipant, webinar_id=event_id)) == []
    claims = await db.all(select(SettlementClaim))
    assert len(claims) == 1
    claim_id = claims[0].id
    operation = required(await db.get(CoinOperation, id=claim_id))
    assert operation.coins == payment_before["paid_coins"] and operation.completed_at is None
    sent = deepcopy(commercial_remote.bodies("register_event"))
    assert len(sent) == 1
    assert (required(await db.get(CommercialHandoff, claim_id=claim_id))).acknowledged_at is None
    assert await event_cancellations.receive(USER, command) == result
    assert commercial_remote.bodies("register_event") == sent
    commercial_remote.handoff_failure = None
    await settlements.recover_settlements()
    await settlements.recover_settlements()
    session.expire_all()
    assert await event_cancellations.receive(USER, command) == result
    assert commercial_remote.bodies("register_event") == sent * 2
    assert len(await db.all(select(SettlementClaim))) == len(await db.all(select(CoinOperation))) == 1
    assert len(await db.all(select(CommercialHandoff))) == 1
    assert (required(await db.get(CommercialHandoff, claim_id=claim_id))).acknowledged_at is not None
    assert (required(await db.get(CoinOperation, id=claim_id))).completed_at is None
    assert immutable_payment(required(await db.get(BookingPayment, id=payment_id))) == payment_before
    assert (required(await db.get(EventCancellation, id=command))).original == receipt
