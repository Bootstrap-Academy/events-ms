"""Actual local cancellation/claim transactions; backend declaration and grant authority are fixtures."""

from copy import deepcopy
from datetime import timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from api.database import db, filter_by, select
from api.models import BookingPayment, EventRightGrant, RetainedEventRight, SettlementClaim, Slot, WebinarParticipant
from api.models.event_cancellation import EventCancellation, EventCancellationClaimEvidence
from api.services import event_cancellations, payment_claims, retained_events, settlements, shop
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data
from api.utils.utc import utcnow
from tests.payment_fixtures import paid_participant
from tests.services.test_event_succession import authorize
from tests.services.test_retained_events import booking, canonical, remote
from tests.services.test_user_deletion import OTHER, THIRD, USER, _slot


async def original(remote, kind="webinar", role="participant", days=9, sibling=False):
    provider, student = (USER, OTHER) if role == "instructor" else (OTHER, USER)
    if kind == "webinar":
        event, booked = await booking(student=student, provider=provider, days=days)
        pid = booked.payment_id
        if sibling:
            await db.add(paid_participant(webinar_id=event.id, user_id=str(uuid4()), paid_coins=777))
    else:
        event = _slot(str(uuid4()), provider, student)
        event.start = utcnow() + timedelta(days=days)
        event.end = event.start + timedelta(hours=1)
        await db.add(event)
        pid = event.payment_id
    remote[USER] = canonical(USER)
    await delete_user_data(USER)
    right = await retained_events.right_for(pid, role)
    return event, right, await db.get(BookingPayment, id=pid)


def declaration(mocker, right, received=None):
    command = str(uuid4())
    payload = {
        "command_id": command,
        "source_subject": USER,
        "right_id": right.id,
        "cancel_identified_contract": True,
        "original_text": "Please cancel this one identified booking.",
    }
    receipt = {
        "protocol": 1,
        "command_id": command,
        "source_subject": USER,
        "right_id": right.id,
        "received_at": (received or utcnow()).isoformat(),
        "purpose": "cancel_identified_event_contract",
        "source": "authenticated_claimant_declaration",
        "declaration": payload,
    }
    previous = shop.commercial.side_effect

    async def call(operation, body):
        if operation == "event_cancellation_authority":
            assert body == {"source_subject": USER, "command_id": command}
            return deepcopy(receipt)
        return await previous(operation, body)

    mocker.patch("api.services.shop.commercial", side_effect=call)
    return command, receipt


@pytest.mark.parametrize("kind", ["webinar", "coaching"])
@pytest.mark.parametrize("role", ["participant", "instructor"])
async def test_exact_declared_right_cancels_current_successor_and_preserves_financial_owner(
    session, remote, mocker, kind, role
):
    event, right, payment = await original(remote, kind, role, sibling=kind == "webinar" and role == "instructor")
    original_payment = (payment.user_id, payment.paid_coins, deepcopy(payment.evidence))
    siblings = {p.payment_id for p in await db.all(filter_by(WebinarParticipant, webinar_id=event.id))} - {payment.id}
    gid, _, _ = await authorize(mocker, right)
    await retained_events.deliver(USER, gid)
    await db.commit()
    command, receipt = declaration(mocker, right)
    result = await event_cancellations.receive(USER, command)
    assert result["state"] == "applied" and result["financial_satisfaction"] is False
    assert result["received_at"] == receipt["received_at"]
    assert result["payment_id"] == payment.id and result["unrelated_bookings_cancelled"] is False
    assert (payment.user_id, payment.paid_coins, payment.evidence) == original_payment
    assert (await db.get(EventRightGrant, id=gid)).state == "withdrawn"
    assert (await db.get(RetainedEventRight, id=right.id)).state == "cancelled"
    if kind == "coaching":
        assert (await db.get(Slot, id=event.id)).booked_by is None
    else:
        assert {p.payment_id for p in await db.all(filter_by(WebinarParticipant, webinar_id=event.id))} == siblings
    claims = await db.all(filter_by(SettlementClaim, user_id=payment.user_id))
    assert len(claims) == 1 and claims[0].coins == payment.paid_coins and claims[0].entitlement == "established"
    assert claims[0].basis["cancellation_command_id"] == command
    assert await event_cancellations.receive(USER, command) == result
    assert len(await db.all(select(EventCancellation))) == 1
    assert len((await export_user_data(USER)).event_cancellations) == 1
    assert (await export_user_data(str(uuid4()))).event_cancellations == []
    assert len(await db.all(select(EventCancellationClaimEvidence))) == 1


async def test_original_receipt_survives_processing_rollback_then_exact_retry(session, remote, mocker):
    event, right, payment = await original(remote)
    command, receipt = declaration(mocker, right)
    real = event_cancellations.process
    failed = mocker.patch.object(
        event_cancellations, "process", AsyncMock(side_effect=RuntimeError("synthetic local failure"))
    )
    with pytest.raises(RuntimeError):
        await event_cancellations.receive(USER, command)
    await db.session.rollback()
    saved = await db.get(EventCancellation, id=command)
    assert saved.original == receipt and saved.result is None
    assert await db.all(select(SettlementClaim)) == []
    failed.side_effect = None
    failed.side_effect = real
    assert (await event_cancellations.receive(USER, command))["state"] == "applied"
    assert (await db.get(EventCancellation, id=command)).original == receipt


async def test_exact_old_command_does_not_cancel_replacement_same_event_booking(session, remote, mocker):
    event, right, payment = await original(remote)
    command, _ = declaration(mocker, right)
    first = await event_cancellations.receive(USER, command)
    replacement = paid_participant(webinar_id=event.id, user_id=USER, paid_coins=42)
    await db.add(replacement)
    await db.commit()
    assert await event_cancellations.receive(USER, command) == first
    assert (await db.get(WebinarParticipant, payment_id=replacement.payment_id)).user_id == USER
    assert not any(replacement.payment_id in row.payment_ids for row in await db.all(select(SettlementClaim)))


async def test_later_cancellation_adds_evidence_to_same_pending_claim_without_rewriting_original(
    session, remote, mocker
):
    event, right, payment = await original(remote)
    batch = await settlements.new_batch("payout")
    old_basis = {"assessment": "original_performance_unknown", "cancellation_inferred_from_erasure": False}
    await payment_claims.credit(
        batch.id,
        event.id,
        payment.user_id,
        [payment],
        "Existing uncertain claim",
        False,
        entitlement="pending_evidence",
        basis=old_basis,
    )
    await db.commit()
    claim = await db.first(select(SettlementClaim))
    original_id, original_created, original_coins = claim.id, claim.created_at, claim.coins
    command, _ = declaration(mocker, right)
    await event_cancellations.receive(USER, command)
    claims = await db.all(select(SettlementClaim))
    assert len(claims) == 1 and claim.id == original_id and claim.created_at == original_created
    assert claim.basis == old_basis and claim.coins == original_coins and claim.entitlement == "established"
    additions = await event_cancellations.claim_evidence(claim.id)
    assert len(additions) == 1 and additions[0]["command_id"] == command
    exported = await export_user_data(USER)
    assert exported.settlement_claims[0].basis == old_basis
    assert exported.settlement_claims[0].cancellation_evidence == additions


@pytest.mark.parametrize("spelling", ["upper", "braced", "compact"])
async def test_original_uuid_spellings_keep_raw_receipt_and_apply_same_right(session, remote, mocker, spelling):
    _, right, _ = await original(remote)
    command, receipt = declaration(mocker, right)
    transform = {
        "upper": str.upper,
        "braced": lambda value: "{" + value + "}",
        "compact": lambda value: value.replace("-", ""),
    }[spelling]
    receipt["right_id"] = transform(right.id)
    receipt["declaration"]["right_id"] = transform(right.id)
    receipt["declaration"]["source_subject"] = transform(USER)
    raw = deepcopy(receipt)
    expected_right = right.id
    first = await event_cancellations.receive(USER, command)
    db.session.expire_all()
    assert first["state"] == "applied" and first["right_id"] == expected_right
    assert await event_cancellations.receive(USER, command) == first
    assert (await db.get(EventCancellation, id=command)).original == raw


@pytest.mark.parametrize("offset", [-4, 2])
@pytest.mark.parametrize("delta", [-1, 0, 1])
async def test_fractional_offset_receipt_qualification_and_export_survive_reload(
    session, remote, mocker, offset, delta
):
    event, right, payment = await original(remote)
    received = utcnow().replace(microsecond=600123).astimezone(timezone(timedelta(hours=offset)))
    right.original = dict(right.original) | {"start": (received + timedelta(days=7, microseconds=delta)).isoformat()}
    await db.commit()
    command, receipt = declaration(mocker, right, received)
    first = await event_cancellations.receive(USER, command)
    claim = await db.first(filter_by(SettlementClaim, event_id=event.id, user_id=payment.user_id))
    assert claim.entitlement == ("established" if delta >= 0 else "pending_evidence")
    # Model metadata may have lower precision on native engines. The immutable
    # authoritative instant remains exact across reload, result and owner export.
    row = await db.get(EventCancellation, id=command)
    row.received_at = received.astimezone(timezone.utc).replace(microsecond=0)
    await db.commit()
    db.session.expire_all()
    assert await event_cancellations.receive(USER, command) == first
    exported = await event_cancellations.export(USER)
    assert exported[0]["received_at"] == received
    assert exported[0]["original"] == receipt
    assert first["received_at"] == received.astimezone(timezone.utc).isoformat()
