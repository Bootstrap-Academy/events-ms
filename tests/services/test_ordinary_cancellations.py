"""Ordinary local receipt/scope transactions; authority and remote effects are fixtures."""

from copy import deepcopy
from datetime import timedelta
from typing import Any, Literal, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db, filter_by, select
from api.models import (
    BookingPayment,
    CoinOperation,
    EmergencyCancel,
    SettlementClaim,
    Slot,
    Webinar,
    WebinarParticipant,
)
from api.models.ordinary_cancellation import OrdinaryEventCancellation
from api.schemas.ordinary_cancellation import CancellationDeclaration, CancellationPreparation
from api.schemas.user import User
from api.services import event_cancellations
from api.services import ordinary_cancellations as ordinary
from api.services import payment_claims, settlements
from api.services.user_export import export_user_data
from api.utils.utc import utcnow
from tests.payment_fixtures import paid_participant
from tests.required import required
from tests.services.test_retained_events import booking
from tests.services.test_user_deletion import OTHER, THIRD, USER, _slot


def actor(subject: str = USER, admin: bool = False) -> User:
    return User(id=subject, admin=admin, email_verified=True)


@pytest.fixture(autouse=True)
def local_effects(mocker: MockerFixture) -> AsyncMock:
    mocker.patch("api.services.ordinary_cancellations.clear_cache", AsyncMock())
    return mocker.patch("api.services.settlements.finish", AsyncMock())


async def setup(
    kind: Literal["webinar", "coaching"] = "webinar", days: int = 9
) -> tuple[Webinar | Slot, BookingPayment]:
    event: Webinar | Slot
    if kind == "webinar":
        event, booked = await booking(days=days)
        payment_id = booked.payment_id
    else:
        event = _slot(str(uuid4()), OTHER, USER)
        event.start = utcnow() + timedelta(days=days)
        event.end = event.start + timedelta(hours=1)
        await db.add(event)
        payment_id = event.payment_id
    await db.commit()
    return event, required(await db.get(BookingPayment, id=payment_id))


async def target(
    event: Any,
    kind: Literal["webinar", "coaching"] = "webinar",
    user: Any = None,
    scope: Literal["auto", "booking", "session"] = "auto",
) -> dict[str, Any]:
    return await ordinary.prepare(user or actor(), event.id, CancellationPreparation(kind=kind, scope=scope))


def statement(prepared: Any, reason: str | None = None) -> CancellationDeclaration:
    return CancellationDeclaration(
        target_id=prepared["id"],
        cancel_selected_scope=True,
        original_text="I cancel the exact scope shown in this confirmation.",
        administration_reason=reason,
    )


@pytest.mark.parametrize("kind", ["webinar", "coaching"])
async def test_exact_receipt_replay_precedes_any_replacement_booking_selection(
    session: AsyncSession, mocker: MockerFixture, kind: Literal["webinar", "coaching"]
) -> None:
    event, payment = await setup(kind)
    prepared = await target(event, kind)
    command, body = str(uuid4()), statement(prepared)
    first = await ordinary.receive(actor(), command, body)
    assert first["state"] == "applied" and first["financial_satisfaction"] is False
    replacement: WebinarParticipant | Slot
    if kind == "webinar":
        replacement = await db.add(paid_participant(webinar_id=event.id, user_id=USER, paid_coins=42))
        replacement_id = replacement.payment_id
    else:
        replacement = _slot(event.id, OTHER, USER)
        replacement_id = replacement.payment_id
        for key in ("payment_id", "booked_by", "event_type", "student_coins", "instructor_coins"):
            setattr(event, key, getattr(replacement, key))
    await db.commit()
    selection = mocker.patch.object(
        ordinary, "locked_event", AsyncMock(side_effect=AssertionError("No current selection on replay"))
    )
    assert await ordinary.receive(actor(), command, body) == first
    selection.assert_not_awaited()
    assert not any(replacement_id in row.payment_ids for row in await db.all(select(SettlementClaim)))
    assert len(await db.all(filter_by(CoinOperation, user_id=USER))) == 1
    assert (required(await db.get(BookingPayment, id=payment.id))).user_id == USER


async def test_first_received_declaration_survives_processing_failure_and_exact_retry(
    session: AsyncSession, mocker: MockerFixture
) -> None:
    event, payment = await setup()
    prepared = await target(event)
    command, body = str(uuid4()), statement(prepared)
    instant = utcnow().replace(microsecond=123456)
    mocker.patch.object(ordinary, "utcnow", return_value=instant)
    real_apply = ordinary.apply
    fail = mocker.patch.object(ordinary, "apply", AsyncMock(side_effect=RuntimeError("local processing interruption")))
    pending = await ordinary.receive(actor(), command, body)
    assert pending["state"] == "received" and pending["received_at"] == instant.isoformat()
    assert await db.all(select(SettlementClaim)) == []
    original = deepcopy((required(await db.get(OrdinaryEventCancellation, id=command))).original)
    fail.side_effect = real_apply
    mocker.patch.object(ordinary, "utcnow", return_value=instant + timedelta(days=20))
    result = await ordinary.receive(actor(), command, body)
    assert result["state"] == "applied" and result["received_at"] == pending["received_at"]
    assert (required(await db.get(OrdinaryEventCancellation, id=command))).original == original
    assert len(await db.all(filter_by(SettlementClaim, user_id=USER))) == 1


@pytest.mark.parametrize("change", ["replace", "new_member", "provider", "period", "remove_parent"])
async def test_stale_prepared_scope_preserves_receipt_and_other_bookings(session: AsyncSession, change: str) -> None:
    event, payment = await setup()
    owner = actor(OTHER) if change == "new_member" else actor()
    prepared = await target(event, user=owner, scope="session" if change == "new_member" else "auto")
    before_payment = deepcopy((payment.user_id, payment.original, payment.evidence))
    if change == "replace":
        booked = await db.get(WebinarParticipant, payment_id=payment.id)
        await db.delete(booked)
        await db.session.flush()
        await db.add(paid_participant(webinar_id=event.id, user_id=USER, paid_coins=42))
    elif change == "new_member":
        await db.add(paid_participant(webinar_id=event.id, user_id=THIRD, paid_coins=42))
    elif change == "provider":
        assert isinstance(event, Webinar)
        event.creator = THIRD
    elif change == "period":
        event.start += timedelta(hours=1)
    else:
        await db.delete(event)
    await db.commit()
    command, body = str(uuid4()), statement(prepared)
    result = await ordinary.receive(owner, command, body)
    assert result["state"] == "resolution_required" and result["booking_changed"] is False
    assert result["received_at"] and result["declaration"]["original_text"] == body.original_text
    assert await ordinary.receive(owner, command, body) == result
    claims = await db.all(select(SettlementClaim))
    if change == "remove_parent":
        assert len(claims) == 1 and claims[0].payment_ids == [payment.id]
        assert claims[0].entitlement == "established" and result["financial_state"] == "pending"
    else:
        assert claims == []
    assert (payment.user_id, payment.original, payment.evidence) == before_payment
    if change == "new_member":
        assert len(await db.all(select(WebinarParticipant))) == 2 and await db.get(Webinar, id=event.id) is not None


@pytest.mark.parametrize("delta", [-1, 0, 1])
async def test_actual_receipt_time_qualifies_seven_day_boundary_with_full_precision(
    session: AsyncSession, mocker: MockerFixture, delta: int
) -> None:
    event, payment = await setup()
    received = utcnow().replace(microsecond=456789)
    event.start = received + timedelta(days=7, microseconds=delta)
    event.end = event.start + timedelta(hours=1)
    await db.commit()
    mocker.patch.object(ordinary, "utcnow", return_value=received - timedelta(days=1))
    prepared = await target(event)
    mocker.patch.object(ordinary, "utcnow", return_value=received)
    command, body = str(uuid4()), statement(prepared)
    result = await ordinary.receive(actor(), command, body)
    claim: SettlementClaim = required(
        cast(SettlementClaim | None, await db.first(filter_by(SettlementClaim, user_id=USER)))
    )
    assert result["state"] == "applied"
    assert claim.entitlement == ("established" if delta >= 0 else "pending_evidence")
    receipt = required(await db.get(OrdinaryEventCancellation, id=command))
    receipt.received_at = received.replace(microsecond=0)
    await db.commit()
    db.session.expire_all()
    assert (await ordinary.status(actor(), command))["received_at"] == received.isoformat()
    evidence = await event_cancellations.claim_evidence(claim.id)
    assert evidence[0]["origin"] == "ordinary_authenticated"
    assert evidence[0]["assessment"]["original_receipt_time"] == received.isoformat()


@pytest.mark.parametrize("kind", ["webinar", "coaching"])
@pytest.mark.parametrize("role", ["provider", "administrator"])
async def test_provider_and_administrator_preserve_actual_payment_and_distinct_waiver(
    session: AsyncSession, kind: Literal["webinar", "coaching"], role: str
) -> None:
    event, payment = await setup(kind, days=1)
    payment.paid_coins = 42
    await db.commit()
    user = actor(OTHER) if role == "provider" else actor(THIRD, admin=True)
    prepared = await target(event, kind, user, "session")
    assert prepared["role"] == role and prepared["recorded_paid_coins"] == 42
    result = await ordinary.receive(
        user, str(uuid4()), statement(prepared, "Recorded intervention" if user.admin else None)
    )
    assert result["state"] == "applied"
    claim: SettlementClaim = required(
        cast(SettlementClaim | None, await db.first(filter_by(SettlementClaim, user_id=USER)))
    )
    assert claim.coins == 42 and claim.entitlement == "established"
    assert claim.basis["cancellation_by"] == role
    assert await EmergencyCancel.exists(OTHER) is (role == "provider")
    assert payment.user_id == USER


async def test_unknown_amount_and_original_instructor_payout_remain_separate(session: AsyncSession) -> None:
    event, payment = await setup("coaching", days=1)
    payment.paid_coins = None
    payment.payout_coins = 560
    await db.commit()
    prepared = await target(event, "coaching")
    assert prepared["recorded_paid_coins"] is None
    result = await ordinary.receive(actor(), str(uuid4()), statement(prepared))
    claims = {row.user_id: row for row in await db.all(select(SettlementClaim))}
    assert result["financial_state"] == "amount_unknown"
    assert claims[USER].coins is None and claims[OTHER].coins == 560
    assert all(row.entitlement == "pending_evidence" for row in claims.values())
    assert await db.get(CoinOperation, id=claims[USER].id) is None


async def test_partial_legacy_aggregate_keeps_basis_and_operation_and_appends_ordinary_evidence(
    session: AsyncSession,
) -> None:
    event, payment = await setup()
    old = await db.add(
        BookingPayment(
            id=str(uuid4()),
            event_id=event.id,
            user_id=USER,
            kind="webinar",
            state="paid",
            paid_coins=42,
            quoted_coins=42,
            description="Earlier original payment",
            original={},
            evidence={},
        )
    )
    batch = await settlements.new_batch("payout")
    claim = await db.add(
        SettlementClaim(
            id=str(uuid4()),
            batch_id=batch.id,
            event_id=event.id,
            user_id=USER,
            payment_ids=[payment.id, old.id],
            amount_field="paid_coins",
            ratio="1",
            coins=1379,
            resolved_at=utcnow(),
            entitlement="pending_evidence",
            description="Original aggregate",
            credit_note=False,
            basis={"original": "unknown performance"},
        )
    )
    await db.session.flush()
    operation = await db.add(
        CoinOperation(
            id=claim.id,
            batch_id=batch.id,
            event_id=event.id,
            user_id=USER,
            coins=1379,
            description=claim.description,
            credit_note=False,
        )
    )
    await db.commit()
    original = deepcopy((claim.id, claim.created_at, claim.basis, claim.payment_ids, operation.id, operation.coins))
    prepared = await target(event)
    result = await ordinary.receive(actor(), str(uuid4()), statement(prepared))
    assert result["state"] == "applied" and result["financial_state"] == "requires_review"
    assert claim.entitlement == "pending_evidence"
    assert (claim.id, claim.created_at, claim.basis, claim.payment_ids, operation.id, operation.coins) == original
    assert len(await db.all(select(SettlementClaim))) == len(await db.all(select(CoinOperation))) == 1
    evidence = await event_cancellations.claim_evidence(claim.id)
    assert len(evidence) == 1 and evidence[0]["assessment"]["payment_ids"] == [payment.id]
    exported = await export_user_data(USER)
    assert exported.settlement_claims[0].cancellation_evidence == evidence
    assert len(exported.ordinary_event_cancellations["declarations"]) == 1
    assert (await export_user_data(THIRD)).ordinary_event_cancellations == {"targets": [], "declarations": []}


async def test_receipt_owner_declaration_and_current_admin_are_not_replaceable(session: AsyncSession) -> None:
    event, _ = await setup()
    prepared = await target(event)
    body, command = statement(prepared), str(uuid4())
    await ordinary.receive(actor(), command, body)
    with pytest.raises(HTTPException) as other:
        await ordinary.status(actor(THIRD, admin=True), command)
    assert other.value.status_code == 404
    changed = body.copy(update={"original_text": "Different declaration"})
    with pytest.raises(HTTPException) as conflicting:
        await ordinary.receive(actor(), command, changed)
    assert conflicting.value.status_code == 409
    with pytest.raises(HTTPException):
        await ordinary.prepare(actor(THIRD), event.id, CancellationPreparation(kind="webinar", scope="session"))
    admin_target = await target(event, user=actor(THIRD, True), scope="session")
    with pytest.raises(HTTPException):
        await ordinary.receive(actor(THIRD), str(uuid4()), statement(admin_target, "Current reason"))


@pytest.mark.parametrize("role", ["participant", "provider", "administrator"])
async def test_background_recovery_uses_accepted_origin_without_fabricated_ordinary_login(
    session: AsyncSession, mocker: MockerFixture, role: str
) -> None:
    from api.database import db_context

    event, payment = await setup()
    user = actor() if role == "participant" else actor(OTHER) if role == "provider" else actor(THIRD, True)
    prepared = await target(event, user=user, scope="auto" if role == "participant" else "session")
    command, body = str(uuid4()), statement(prepared, "Actual intervention" if role == "administrator" else None)
    real_apply = ordinary.apply
    patch = mocker.patch.object(ordinary, "apply", AsyncMock(side_effect=RuntimeError("after receipt commit")))
    saved = await ordinary.receive(user, command, body)
    assert saved["state"] == "received"
    original = deepcopy((required(await db.get(OrdinaryEventCancellation, id=command))).original)
    patch.side_effect = real_apply
    # Worker uses the accepted proof, not a synthetic User or a later login.
    mocker.patch.object(ordinary, "apply", AsyncMock(side_effect=AssertionError("worker must use saved declaration")))
    await ordinary.recover()
    async with db_context():
        row = required(await db.get(OrdinaryEventCancellation, id=command))
        assert required(row.result)["state"] == "applied" and row.original == original
        assert row.last_attempt_at is not None
        assert await EmergencyCancel.exists(OTHER) is (role == "provider")
    await ordinary.recover()
    async with db_context():
        assert len(await db.all(select(SettlementClaim))) == 1


@pytest.mark.parametrize("role", ["participant", "provider"])
async def test_timely_original_receipt_after_application_failure_and_parent_cleanup_assesses_surviving_payment(
    session: AsyncSession, mocker: MockerFixture, role: str
) -> None:
    event, payment = await setup()
    owner = actor() if role == "participant" else actor(OTHER)
    prepared = await target(event, user=owner)
    received = event.start - timedelta(days=8)
    mocker.patch.object(ordinary, "utcnow", return_value=received)
    real_apply = ordinary.apply
    fail = mocker.patch.object(ordinary, "apply", AsyncMock(side_effect=RuntimeError("first application failed")))
    command, body = str(uuid4()), statement(prepared)
    assert (await ordinary.receive(owner, command, body))["state"] == "received"
    # The normal cleanup's independent remuneration claim and retained Payment survive parent deletion.
    batch = await settlements.new_batch("payout")
    await payment_claims.credit(
        batch.id,
        event.id,
        OTHER,
        [payment],
        "Original remuneration",
        True,
        field="payout_coins",
        entitlement="pending_evidence",
        basis={"actual_performance": "unknown"},
    )
    await db.delete(event)
    await db.commit()
    fail.side_effect = real_apply
    mocker.patch.object(ordinary, "utcnow", return_value=received + timedelta(days=20))
    result = await ordinary.receive(owner, command, body)
    claim: SettlementClaim = required(
        cast(SettlementClaim | None, await db.first(filter_by(SettlementClaim, user_id=USER)))
    )
    assert result["state"] == "resolution_required" and result["booking_changed"] is False
    assert claim.coins == payment.paid_coins and claim.entitlement == "established"
    evidence = await event_cancellations.claim_evidence(claim.id)
    assert evidence[0]["assessment"]["original_receipt_time"] == received.isoformat()
    assert await db.get(Webinar, id=event.id) is None
    assert (await ordinary.receive(owner, command, body))["command_id"] == command
    assert len(await db.all(filter_by(SettlementClaim, user_id=USER))) == 1


async def test_whole_session_collects_original_instructor_aggregate_components_once(session: AsyncSession) -> None:
    event, payment = await setup(days=-1)
    other_seat = await db.add(paid_participant(webinar_id=event.id, user_id=THIRD, paid_coins=42))
    other_payment = required(await db.get(BookingPayment, id=other_seat.payment_id))
    payment.payout_coins, other_payment.payout_coins = 936, 29
    # Admitted legacy state: one instructor remuneration claim covers several
    # original student orders. Payers are distinct and never rewritten.
    batch = await settlements.new_batch("legacy")
    claim = required(
        await db.add(
            SettlementClaim(
                id=str(uuid4()),
                batch_id=batch.id,
                event_id=event.id,
                user_id=OTHER,
                payment_ids=[payment.id, other_payment.id],
                amount_field="payout_coins",
                ratio="1",
                description="Original aggregate remuneration",
                credit_note=True,
                entitlement="pending_evidence",
                basis={"legacy": True},
            )
        )
    )
    await payment_claims.resolve(claim, [payment, other_payment])
    await db.commit()
    original = deepcopy((claim.id, claim.payment_ids, claim.basis, claim.coins))
    original_operation = deepcopy((required(await db.get(CoinOperation, id=claim.id))).coins)
    prepared = await target(event, user=actor(OTHER), scope="session")
    command = str(uuid4())
    result = await ordinary.receive(actor(OTHER), command, statement(prepared))
    assert result["state"] == "applied"
    rows = await event_cancellations.claim_evidence(claim.id)
    assert len(rows) == 1 and rows[0]["assessment"]["payment_ids"] == sorted([payment.id, other_payment.id])
    assert (
        claim.entitlement == "pending_evidence" and (claim.id, claim.payment_ids, claim.basis, claim.coins) == original
    )
    assert (required(await db.get(CoinOperation, id=claim.id))).coins == original_operation
    assert len(await db.all(filter_by(SettlementClaim, user_id=OTHER))) == 1
    assert len(await db.all(select(CoinOperation))) == 3
    assert payment.user_id == USER and other_payment.user_id == THIRD


async def test_recovery_failure_rotates_behind_other_pending_receipts(
    session: AsyncSession, mocker: MockerFixture
) -> None:
    from api.database import db_context
    from api.models.ordinary_cancellation import OrdinaryCancellationTarget

    now = utcnow()
    for i in range(12):
        ident = str(uuid4())
        await db.add(OrdinaryCancellationTarget(id=ident, actor_id=USER, prepared_at=now, original={}))
        await db.add(
            OrdinaryEventCancellation(
                id=ident,
                actor_id=USER,
                target_id=ident,
                received_at=now - timedelta(days=1, seconds=12 - i),
                original={},
                result=None,
            )
        )
    await db.commit()
    invoked = []

    async def fail(actor_id: str, command: str) -> None:
        invoked.append(command)
        raise RuntimeError("recoverable application failure")

    mocker.patch.object(ordinary, "apply_saved", side_effect=fail)
    await ordinary.recover()
    assert len(invoked) == 10
    first = list(invoked)
    await ordinary.recover()
    assert len(invoked) == 20 and invoked[10] not in first and invoked[11] not in first
    async with db_context():
        assert all(row.result is None for row in await db.all(select(OrdinaryEventCancellation)))


async def test_ordinary_and_retained_receipts_with_same_uuid_keep_distinct_provenance_and_authority(
    session: AsyncSession,
) -> None:
    from api.models.event_cancellation import EventCancellation, EventCancellationClaimEvidence
    from api.models.ordinary_cancellation import OrdinaryCancellationClaimEvidence

    event, payment = await setup()
    prepared = await target(event)
    command = str(uuid4())
    result = await ordinary.receive(actor(), command, statement(prepared))
    claim: SettlementClaim = required(
        cast(SettlementClaim | None, await db.first(filter_by(SettlementClaim, user_id=USER)))
    )
    ordinary_bytes = deepcopy(
        (required(await db.get(OrdinaryCancellationClaimEvidence, command_id=command, claim_id=claim.id))).assessment
    )
    with pytest.raises(ValueError, match="durable original declaration"):
        await event_cancellations.observe_claim(claim, command, [payment], "established", {})
    assert await db.all(select(EventCancellationClaimEvidence)) == []
    # Independent retained-origin fixture uses its own immutable authority row,
    # even when the UUID coincides with an ordinary command.
    await db.add(
        EventCancellation(
            id=command,
            source_subject=USER,
            right_id=str(uuid4()),
            received_at=utcnow(),
            observed_at=utcnow(),
            original={"received_at": result["received_at"], "source": "authenticated_claimant_declaration"},
            result=None,
        )
    )
    await event_cancellations.observe_claim(
        claim, command, [payment], "established", {"amount_field": "paid_coins", "ratio": 1}
    )
    rows = await event_cancellations.claim_evidence(claim.id)
    assert {row["origin"] for row in rows} == {"ordinary_authenticated", "retained_claimant"}
    assert len(rows) == 2 and all(row["command_id"] == command for row in rows)
    assert (
        required(await db.get(OrdinaryCancellationClaimEvidence, command_id=command, claim_id=claim.id))
    ).assessment == ordinary_bytes


@pytest.mark.parametrize("history", ["completed", "uncertain"])
async def test_reassessment_keeps_original_operation_history_and_does_not_create_another_credit(
    session: AsyncSession, history: str
) -> None:
    event, payment = await setup()
    batch = await settlements.new_batch("legacy")
    await payment_claims.credit(
        batch.id,
        event.id,
        USER,
        [payment],
        "Original return",
        False,
        entitlement="pending_evidence",
        basis={"original": "unchanged"},
    )
    claim: SettlementClaim = required(
        cast(SettlementClaim | None, await db.first(filter_by(SettlementClaim, user_id=USER)))
    )
    operation = required(await db.get(CoinOperation, id=claim.id))
    operation.completed_at = utcnow() if history == "completed" else None
    operation.last_error = "PriorOutcomeUnknown" if history == "uncertain" else None
    await db.commit()
    before = deepcopy({column.name: getattr(operation, column.name) for column in operation.__table__.columns})
    prepared = await target(event)
    result = await ordinary.receive(actor(), str(uuid4()), statement(prepared))
    assert result["financial_state"] == ("historical_application" if history == "completed" else "uncertain")
    assert {column.name: getattr(operation, column.name) for column in operation.__table__.columns} == before
    assert len(await db.all(select(CoinOperation))) == 1 and claim.basis == {"original": "unchanged"}


def test_new_schema_preserves_retained_fk_and_original_rows_without_backfill() -> None:
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from sqlalchemy import create_engine, inspect, text

    path = Path("alembic/versions/2026_09_09_1000-l3ordinarycancel001_ordinary_receipts.py")
    spec = importlib.util.spec_from_file_location("ordinary_migration_fixture", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE events_settlement_claims (id VARCHAR(36) PRIMARY KEY, original TEXT)"))
        connection.execute(
            text("CREATE TABLE events_cancellation_declarations (id VARCHAR(36) PRIMARY KEY, original TEXT)")
        )
        connection.execute(
            text(
                "CREATE TABLE events_cancellation_claim_evidence (command_id VARCHAR(36) "
                "REFERENCES events_cancellation_declarations(id), "
                "claim_id VARCHAR(36) REFERENCES events_settlement_claims(id))"
            )
        )
        connection.execute(
            text("INSERT INTO events_cancellation_declarations VALUES ('original', 'exact retained bytes')")
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        schema = inspect(connection)
        assert (
            next(
                fk
                for fk in schema.get_foreign_keys("events_cancellation_claim_evidence")
                if fk["constrained_columns"] == ["command_id"]
            )["referred_table"]
            == "events_cancellation_declarations"
        )
        assert (
            next(
                fk
                for fk in schema.get_foreign_keys("events_ordinary_cancellation_claim_evidence")
                if fk["constrained_columns"] == ["command_id"]
            )["referred_table"]
            == "events_ordinary_cancellations"
        )
        assert (
            connection.execute(text("SELECT original FROM events_cancellation_declarations")).scalar()
            == "exact retained bytes"
        )
        assert connection.execute(text("SELECT COUNT(*) FROM events_ordinary_cancellations")).scalar() == 0
        with pytest.raises(RuntimeError, match="Preserve actual"):
            migration.downgrade()
    engine.dispose()


async def test_late_local_failure_rolls_back_claim_booking_and_evidence_but_keeps_first_receipt(
    session: AsyncSession, mocker: MockerFixture
) -> None:
    from api.models.settlement import SettlementBatch

    event, payment = await setup()
    prepared = await target(event)
    command, body = str(uuid4()), statement(prepared)
    original = deepcopy(payment.original)
    payment_id = payment.id
    record = ordinary.record_assessments
    fail = mocker.patch.object(
        ordinary, "record_assessments", AsyncMock(side_effect=RuntimeError("after claim insertion"))
    )
    result = await ordinary.receive(actor(), command, body)
    assert result["state"] == "received"
    assert await db.all(select(SettlementClaim)) == [] and await db.all(select(SettlementBatch)) == []
    assert await db.get(WebinarParticipant, webinar_id=prepared["event_id"], user_id=USER) is not None
    assert (required(await db.get(BookingPayment, id=payment_id))).original == original
    fail.side_effect = record
    result = await ordinary.receive(actor(), command, body)
    assert result["state"] == "applied"
    assert len(await db.all(select(SettlementClaim))) == 1


@pytest.mark.parametrize("kind", ["webinar", "coaching"])
async def test_preparation_cannot_select_another_subjects_booking_or_change_role(
    session: AsyncSession, kind: Literal["webinar", "coaching"]
) -> None:
    event, payment = await setup(kind)
    with pytest.raises(HTTPException) as exc:
        await target(event, kind, actor(THIRD))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await target(event, kind, actor(), "session")
    assert exc.value.status_code == 403
    assert await db.all(select(OrdinaryEventCancellation)) == []
    assert (required(await db.get(BookingPayment, id=payment.id))).user_id == USER
