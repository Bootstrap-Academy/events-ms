"""Four real local recovery/transaction interruptions; external delivery is synthetic.

No application server is started. Only the fixture-owned file SQLite engine is
disposed; production recovery, financial writes, transaction cleanup and worker
functions run. Delegating observers add scheduling barriers without fake SQL.
"""

import asyncio
import json
from contextvars import ContextVar
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import AsyncClient, MockTransport, Response
from sqlalchemy import event as sql_event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from api import app
from api.database import db, db_context, filter_by, select
from api.models import BookingPayment, CoinOperation, SettlementClaim, WebinarParticipant
from api.models.booking_contract import BookingContract
from api.models.booking_payment import CommercialBatchClaim, CommercialHandoff
from api.models.ordinary_cancellation import OrdinaryCancellationClaimEvidence, OrdinaryEventCancellation
from api.models.settlement import SettlementBatch
from api.schemas.ordinary_cancellation import CancellationDeclaration, CancellationPreparation
from api.schemas.user import User
from api.services import ordinary_cancellations as ordinary, settlements
from api.services.internal import InternalService
from tests.payment_fixtures import paid_participant
from tests.services.test_retained_events import booking
from tests.services.test_user_deletion import OTHER, USER


def columns(row):
    return deepcopy({column.name: getattr(row, column.name) for column in row.__table__.columns})


@pytest.fixture
async def isolated(database, tmp_path, monkeypatch, mocker):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ordinary-interruption.sqlite'}")
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "committed_read_engine", None)
    monkeypatch.setattr(db, "_session", ContextVar("qa_session", default=None))
    monkeypatch.setattr(db, "_close_event", ContextVar("qa_close", default=None))
    await db.create_tables()
    state = SimpleNamespace(
        engine=engine,
        trace=[],
        commits=[],
        rollbacks=[],
        active={},
        tasks=[],
        clients=[],
        requests=[],
        command=None,
        stage=None,
        enabled=False,
        reached=asyncio.Event(),
        interrupted=None,
        worker=None,
        cycle=asyncio.Event(),
        booking_close=None,
        disposals=0,
    )
    listeners = []

    def listen(target, name, callback):
        sql_event.listen(target, name, callback)
        listeners.append((target, name, callback))

    def checkout(connection, record, proxy):
        record.info["qa_record"] = id(record)
        state.active[id(record)] = None

    def begin(session, transaction, connection):
        if connection.engine is engine.sync_engine:
            connection.info["qa_session"] = id(session)
            record_id = connection.info["qa_record"]
            state.active[record_id] = id(session)
            state.trace.append(["begin", id(session), id(transaction), record_id])

    def checkin(connection, record):
        state.trace.append(["checkin", record.info.get("qa_session"), id(record)])
        state.active.pop(id(record), None)

    def rollback(session, transaction):
        if session.bind is engine.sync_engine:
            state.rollbacks.append((id(session), id(transaction)))
            state.trace.append(["rollback", id(session), id(transaction)])

    def disposed(value):
        assert value is engine.sync_engine
        state.disposals += 1

    listen(engine.sync_engine, "checkout", checkout)
    listen(engine.sync_engine, "checkin", checkin)
    listen(engine.sync_engine, "engine_disposed", disposed)
    listen(Session, "after_begin", begin)
    listen(Session, "after_soft_rollback", rollback)
    real_commit = AsyncSession.commit

    async def observed_commit(session):
        transaction = session.sync_session.get_transaction()
        await real_commit(session)
        sid = id(session.sync_session)
        state.commits.append((sid, id(transaction) if transaction else None))
        state.trace.append(["commit_completed", sid, id(transaction) if transaction else None])
        command = session.info.get("qa_accept_command")
        if command:
            matches = [
                row
                for row in session.identity_map.values()
                if isinstance(row, OrdinaryEventCancellation) and row.id == command
            ]
            assert len(matches) == 1 and matches[0].result is None and matches[0].last_attempt_at is None
            session.info.pop("qa_accept_command")
            state.trace.append(["accepted_commit_completed", command, sid])
            # The actual driver/session commit has returned. Cancellation at the
            # next scheduling point cannot be mistaken for precommit acceptance.
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        if session.info.get("qa_booking_scan"):
            state.cycle.set()

    monkeypatch.setattr(AsyncSession, "commit", observed_commit)

    async def pause(stage, claim_id, batch_id):
        session = db.session
        saved = [
            row
            for row in session.identity_map.values()
            if isinstance(row, OrdinaryEventCancellation) and row.id == state.command
        ]
        assert len(saved) == 1
        transaction = session.sync_session.get_transaction()
        assert (saved[0].result is None) == (stage == "precommit")
        assert (transaction is not None) == (stage == "precommit")
        if stage == "postcommit":
            assert saved[0].result["claim_ids"] == [claim_id] and saved[0].result["batch_id"] == batch_id
        state.interrupted = SimpleNamespace(
            session=session,
            sid=id(session.sync_session),
            transaction=id(transaction) if transaction else None,
            close_event=db._close_event.get(),
            command=state.command,
            claim_id=claim_id,
            batch_id=batch_id,
            trace_index=len(state.trace),
            connections=[record for record, owner in state.active.items() if owner == id(session.sync_session)],
        )
        assert bool(state.interrupted.connections) == (stage == "precommit")
        state.trace.append(
            ["barrier", stage, state.command, claim_id, batch_id, state.interrupted.sid, state.interrupted.transaction]
        )
        state.reached.set()
        await asyncio.Event().wait()

    real_all = db.all

    async def observed_all(query):
        result = await real_all(query)
        entity = query.column_descriptions[0].get("entity") if query.column_descriptions else None
        if asyncio.current_task() is state.worker and entity is BookingContract:
            db.session.info["qa_booking_scan"] = True
            state.booking_close = db._close_event.get()
        if state.enabled and state.stage == "precommit" and entity is CommercialBatchClaim and result:
            saved = [
                row
                for row in db.session.identity_map.values()
                if isinstance(row, OrdinaryEventCancellation) and row.id == state.command
            ]
            if saved and saved[0].result is None:
                # The original read has exhausted its real result and autoflush.
                # Verify the selected evidence exists in this actual transaction.
                evidence = (
                    (await db.session.execute(filter_by(OrdinaryCancellationClaimEvidence, command_id=state.command)))
                    .scalars()
                    .all()
                )
                assert len(evidence) == 1 and len(result) == 1
                assert evidence[0].claim_id == result[0].claim_id
                claim = (await db.session.execute(filter_by(SettlementClaim, id=result[0].claim_id))).scalar_one()
                assert claim.basis["ordinary_cancellation_command_id"] == state.command
                assert saved[0].last_attempt_at is not None
                await pause("precommit", claim.id, result[0].batch_id)
        return result

    monkeypatch.setattr(db, "all", observed_all)
    mocker.patch.object(InternalService.SHOP, "_value_", "http://services.synthetic.test")
    mocker.patch.object(InternalService, "_get_token", return_value="synthetic-internal-proof")

    async def http(request):
        body = json.loads(request.content) if request.content else None
        state.requests.append([request.method, request.url.path, deepcopy(body)])
        if request.url.path.endswith("/claims/event_cancellation_pending"):
            return Response(200, content=b"[]")
        if request.url.path.endswith("/claims/register_event"):
            claim_id = body["obligation_id"]
            if (
                state.enabled
                and state.stage == "postcommit"
                and body["observation"]["basis"].get("ordinary_cancellation_command_id") == state.command
            ):
                handoff = [
                    row
                    for row in db.session.identity_map.values()
                    if isinstance(row, CommercialHandoff) and row.claim_id == claim_id
                ]
                assert len(handoff) == 1 and handoff[0].payload == body and handoff[0].acknowledged_at is None
                receipt = next(
                    row
                    for row in db.session.identity_map.values()
                    if isinstance(row, OrdinaryEventCancellation) and row.id == state.command
                )
                await pause("postcommit", claim_id, receipt.result["batch_id"])
            return Response(
                200,
                json={
                    "protocol": 1,
                    "obligation_id": claim_id,
                    "disposition": "claim_preserved",
                    "financial_satisfaction": False,
                },
            )
        if "/purchase-fulfillment/events/" in request.url.path:
            assert body == state.fulfillment
            return Response(200, json={"accepted": True})
        if request.method == "GET" and "/users/" in request.url.path:
            return Response(200, json={"email_verified": True, "email": "fixture@example.test"})
        raise AssertionError(f"Unexpected external request: {request.method} {request.url.path}")

    def client(*args, **kwargs):
        value = AsyncClient(*args, **kwargs, transport=MockTransport(http))
        state.clients.append(value)
        return value

    mocker.patch("api.services.internal.AsyncClient", side_effect=client)
    mocker.patch("api.utils.cache.redis.keys", new_callable=AsyncMock, return_value=[])
    mocker.patch("api.utils.email.check_email_deliverability", new_callable=AsyncMock, return_value=True)
    state.smtp = mocker.patch(
        "api.utils.email.aiosmtplib.send", new_callable=AsyncMock, return_value=({}, "synthetic accepted")
    )
    try:
        yield state
    finally:
        for task in state.tasks:
            task.cancel()
        await asyncio.gather(*state.tasks, return_exceptions=True)
        assert all(value.is_closed for value in state.clients)
        assert db.engine is engine and db.committed_read_engine is None
        await engine.dispose()
        for target, name, callback in reversed(listeners):
            sql_event.remove(target, name, callback)


async def accepted_original(state):
    user = User(id=USER, admin=False, email_verified=True)
    async with db_context():
        event, seat = await booking()
        prepared = await ordinary.prepare(user, event.id, CancellationPreparation(kind="webinar", scope="auto"))
        payment = await db.get(BookingPayment, id=seat.payment_id)
        event_id, payment_id, payment_before = event.id, payment.id, columns(payment)
    command = str(uuid4())
    body = CancellationDeclaration(
        target_id=prepared["id"],
        cancel_selected_scope=True,
        original_text="Cancel only this displayed original booking.",
    )

    async def intake():
        async with db_context():
            db.session.info["qa_accept_command"] = command
            await ordinary.receive(user, command, body)

    task = asyncio.create_task(intake())
    state.tasks.append(task)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    async with db_context():
        saved = await db.get(OrdinaryEventCancellation, id=command)
        assert saved is not None and saved.result is None and saved.last_attempt_at is None
        original = deepcopy(saved.original)
        assert original["actor_id"] == USER and original["target"]["orders"][0]["payment_id"] == payment_id
        assert await db.all(filter_by(SettlementClaim, event_id=event_id)) == []
    return SimpleNamespace(
        command=command,
        body=body,
        user=user,
        event_id=event_id,
        payment_id=payment_id,
        payment_before=payment_before,
        original=original,
    )


def start_worker(state, monkeypatch):
    state.cycle, state.booking_close = asyncio.Event(), None
    state.worker = asyncio.create_task(app.confirmation_loop())
    companions = [asyncio.create_task(asyncio.Event().wait()) for _ in range(2)]
    state.tasks.extend([state.worker, *companions])
    monkeypatch.setattr(app.app.state, "confirmation_task", state.worker, raising=False)
    monkeypatch.setattr(app.app.state, "cleanup_task", companions[0], raising=False)
    monkeypatch.setattr(app.app.state, "benefit_task", companions[1], raising=False)
    return [state.worker, *companions]


async def completed_cycle(state):
    await asyncio.wait_for(state.cycle.wait(), 12)
    assert state.booking_close is not None
    await asyncio.wait_for(state.booking_close.wait(), 2)


async def cancel_tasks(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
    assert all(task.cancelled() for task in tasks)


@pytest.mark.parametrize("stage", ["precommit", "postcommit"])
@pytest.mark.parametrize("interruption", ["deadline", "shutdown"])
async def test_real_ordinary_recovery_interruption_keeps_original_receipt_and_resumes_once(
    isolated, monkeypatch, stage, interruption
):
    state = isolated
    primary = await accepted_original(state)
    secondary = await accepted_original(state)
    async with db_context():
        # Existing fulfilled, closed contract awaiting its report acknowledgment;
        # this control is continued reporting, not a new SQLite booking admission.
        state.report_id = str(uuid4())
        state.fulfillment = {"protocol": 1, "order_id": state.report_id, "original": "synthetic retained proof"}
        await db.add(
            BookingContract(
                id=state.report_id,
                user_id=OTHER,
                event_id=str(uuid4()),
                kind="webinar",
                offer={},
                state="ready",
                closed=True,
                fulfillment=state.fulfillment,
                reported=False,
            )
        )
    state.command, state.stage, state.enabled = primary.command, stage, True
    tasks = start_worker(state, monkeypatch)
    await asyncio.wait_for(state.reached.wait(), 5)
    interrupted = state.interrupted
    assert interrupted.command == primary.command and interrupted.close_event is not None
    if interruption == "shutdown":
        assert db.engine is state.engine and db.committed_read_engine is None
        await asyncio.wait_for(app.on_shutdown(), 3)
        assert all(task.cancelled() for task in tasks) and state.disposals == 1
    else:
        # No shortened/mocked timeout or synthetic family callback: the actual
        # ten-second production deadline must let retained and booking work run.
        await completed_cycle(state)
        assert not state.worker.done() and not state.worker.cancelled()
        await cancel_tasks(tasks)
        assert state.disposals == 0
    assert interrupted.close_event.is_set()
    assert not interrupted.session.in_transaction()
    assert interrupted.sid not in state.active.values()
    after_barrier = state.trace[interrupted.trace_index + 1 :]
    if stage == "precommit":
        assert ["rollback", interrupted.sid, interrupted.transaction] in after_barrier
        assert all(["checkin", interrupted.sid, record] in after_barrier for record in interrupted.connections)
    else:
        assert any(sid == interrupted.sid and transaction is not None for sid, transaction in state.commits)

    async with db_context():
        saved = await db.get(OrdinaryEventCancellation, id=primary.command)
        assert saved.original == primary.original and saved.last_attempt_at is not None
        assert columns(await db.get(BookingPayment, id=primary.payment_id)) == primary.payment_before
        claims = await db.all(filter_by(SettlementClaim, event_id=primary.event_id))
        if stage == "precommit":
            assert saved.result is None and claims == []
            assert await db.all(filter_by(SettlementBatch, event_id=primary.event_id)) == []
            assert await db.all(filter_by(OrdinaryCancellationClaimEvidence, command_id=primary.command)) == []
            assert await db.get(CoinOperation, id=interrupted.claim_id) is None
            assert await db.get(CommercialHandoff, claim_id=interrupted.claim_id) is None
            assert await db.all(filter_by(CommercialBatchClaim, batch_id=interrupted.batch_id)) == []
            assert (await db.get(WebinarParticipant, payment_id=primary.payment_id)).user_id == USER
        else:
            assert saved.result["state"] == "applied" and saved.result["claim_ids"] == [interrupted.claim_id]
            assert len(claims) == 1 and claims[0].id == interrupted.claim_id
            handoff = await db.get(CommercialHandoff, claim_id=interrupted.claim_id)
            assert handoff is not None and handoff.acknowledged_at is None
            assert handoff.payload["observation"]["basis"]["ordinary_cancellation_command_id"] == primary.command
            assert (await db.get(SettlementBatch, id=interrupted.batch_id)).notified_at is None
            assert (await db.get(CoinOperation, id=interrupted.claim_id)).completed_at is None
            assert await db.get(WebinarParticipant, payment_id=primary.payment_id) is None

    state.enabled = False
    tasks = start_worker(state, monkeypatch)
    await completed_cycle(state)
    await cancel_tasks(tasks)
    # Saved-result ordinary receipts are excluded from ordinary recovery. Real
    # settlement recovery must finish their durable pending handoff/notice.
    await settlements.recover_settlements()
    async with db_context():
        saved = await db.get(OrdinaryEventCancellation, id=primary.command)
        first_result = deepcopy(saved.result)
        [claim] = await db.all(filter_by(SettlementClaim, event_id=primary.event_id))
        claim_id, batch_id = claim.id, claim.batch_id
        assert saved.original == primary.original and first_result["claim_ids"] == [claim_id]
        assert claim.user_id == USER and claim.payment_ids == [primary.payment_id] and claim.coins == 1337
        assert (await db.get(OrdinaryEventCancellation, id=secondary.command)).result["state"] == "applied"
        assert (await db.get(BookingContract, id=state.report_id)).reported is True
        if stage == "postcommit":
            assert (claim_id, batch_id) == (interrupted.claim_id, interrupted.batch_id)
        else:
            assert await db.get(SettlementClaim, id=interrupted.claim_id) is None
        evidence = await db.all(filter_by(OrdinaryCancellationClaimEvidence, command_id=primary.command))
        assert len(evidence) == 1 and evidence[0].claim_id == claim_id
        evidence_before, claim_before = columns(evidence[0]), columns(claim)
        operation_before = columns(await db.get(CoinOperation, id=claim_id))
        assert operation_before["completed_at"] is None
        assert (await db.get(CommercialHandoff, claim_id=claim_id)).acknowledged_at is not None
        assert (await db.get(SettlementBatch, id=batch_id)).notified_at is not None
        replacement = await db.add(paid_participant(webinar_id=primary.event_id, user_id=USER, paid_coins=42))
        replacement_id = replacement.payment_id
    await ordinary.recover()
    await settlements.recover_settlements()
    async with db_context():
        replay = await ordinary.receive(primary.user, primary.command, primary.body)
        assert replay["state"] == "applied" and replay["financial_satisfaction"] is False
        assert replay["received_at"] == primary.original["received_at"]
        saved = await db.get(OrdinaryEventCancellation, id=primary.command)
        assert saved.original == primary.original and saved.result == first_result
        [claim] = await db.all(filter_by(SettlementClaim, event_id=primary.event_id))
        assert columns(claim) == claim_before and claim.payment_ids == [primary.payment_id]
        assert columns(await db.get(CoinOperation, id=claim_id)) == operation_before
        [evidence] = await db.all(filter_by(OrdinaryCancellationClaimEvidence, command_id=primary.command))
        assert columns(evidence) == evidence_before
        assert columns(await db.get(BookingPayment, id=primary.payment_id)) == primary.payment_before
        assert (await db.get(WebinarParticipant, payment_id=replacement_id)).user_id == USER
        assert len(await db.all(select(OrdinaryEventCancellation))) == 2
    assert state.smtp.await_count >= 4
    print(
        json.dumps(
            {
                "interruption": interruption,
                "stage": stage,
                "command": primary.command,
                "interrupted_session": interrupted.sid,
                "transaction": interrupted.transaction,
                "staged_claim": interrupted.claim_id,
                "final_claim": claim_id,
                "real_close_event": interrupted.close_event.is_set(),
                "disposals_before_teardown": state.disposals,
                "trace": [
                    row
                    for row in state.trace
                    if row[0] in {"barrier", "accepted_commit_completed"} or row[1] == interrupted.sid
                ],
            },
            default=str,
        )
    )
