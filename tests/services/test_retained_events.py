"""Actual local erasure/cleanup transactions; remote canonical receipts are fixtures."""

from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.database import db, select
from api.models import (
    CalendarToken,
    RetainedEventErasure,
    RetainedEventRight,
    SettlementClaim,
    Slot,
    Webinar,
    WebinarParticipant,
)
from api.models.webinars import clean_old_webinars
from api.services import commercial, retained_events
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data
from api.utils.utc import utcnow
from tests.payment_fixtures import paid_participant
from tests.services.test_user_deletion import OTHER, THIRD, USER, _slot, _webinar, _weekly_slot


def canonical(subject, *, declaration=None):
    return {
        "protocol": 1,
        "subject": subject,
        "case_id": str(uuid4()),
        "request": {
            "id": str(uuid4()),
            "source": "authenticated_service_receipt",
            "received_at": utcnow().isoformat(),
            "evidence": {"declaration": declaration or {"paid_contract_intent": "none"}},
        },
    }


@pytest.fixture
def remote(mocker):
    receipts = {}

    async def call(operation, payload):
        if operation == "erasure":
            return receipts.get(payload["subject"])
        if operation == "inventory":
            return {"accepted": True, "financial_satisfaction": False}
        raise AssertionError(operation)

    mocker.patch("api.services.shop.commercial", side_effect=call)
    mocker.patch("api.services.settlements.finish", AsyncMock())
    mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    return receipts


async def booking(student=USER, provider=OTHER, days=9):
    event = _webinar(str(uuid4()), provider)
    event.start = utcnow() + timedelta(days=days)
    event.end = event.start + timedelta(hours=1)
    await db.add(event)
    booked = paid_participant(webinar_id=event.id, user_id=student, paid_coins=1337)
    await db.add(booked)
    return event, booked


async def test_data_erasure_preserves_booking_and_owner_evidence(session, remote):
    event, booked = await booking()
    receipt = canonical(USER)
    remote[USER] = receipt
    await db.add(CalendarToken(user_id=USER, token="synthetic-token"))
    await delete_user_data(USER)
    assert await db.get(WebinarParticipant, webinar_id=event.id, user_id=USER) is not None
    assert await db.get(CalendarToken, user_id=USER) is None
    rights = await db.all(select(RetainedEventRight))
    assert len(rights) == 1 and rights[0].source_subject == USER and rights[0].current_subject is None
    assert rights[0].original["paid_coins"] == 1337
    assert rights[0].original["payment_or_performance_inferred"] is False
    assert await db.all(select(SettlementClaim)) == []
    exported = await export_user_data(USER)
    assert exported.retained_event_rights["rights"][0]["id"] == rights[0].id
    assert (await export_user_data(THIRD)).retained_event_rights == {
        "rights": [],
        "erasures": [],
        "grants": [],
        "booking_reservations": {},
        "subject_guard": None,
    }
    await delete_user_data(USER)
    assert len(await db.all(select(RetainedEventRight))) == 1
    assert len(await db.all(select(RetainedEventErasure))) == 1
    assert (await db.get(commercial.CommercialErasureReceipt, subject=USER)).canonical == receipt


async def test_provider_erasure_preserves_capacity_and_detaches_recurring_rules(session, remote):
    event, booked = await booking(student=OTHER, provider=USER)
    weekly = _weekly_slot(str(uuid4()), USER)
    await db.add(weekly)
    slot = _slot(str(uuid4()), USER, OTHER, weekly.id)
    await db.add(slot)
    remote[USER] = canonical(USER)
    await delete_user_data(USER)
    assert (await db.get(Webinar, id=event.id)).closed_to_new_bookings is True
    assert await db.get(WebinarParticipant, webinar_id=event.id, user_id=OTHER) is not None
    current = await db.get(Slot, id=slot.id)
    assert current.booked_by == OTHER and current.weekly_slot_id is None
    assert await db.get(type(weekly), id=weekly.id) is None
    assert len(await db.all(select(RetainedEventRight))) == 2
    assert await db.all(select(SettlementClaim)) == []


@pytest.mark.parametrize("provider_cancel", [False, True])
async def test_identified_cancellation_uses_original_declaration_time(session, remote, provider_cancel):
    event, booked = await booking(
        student=OTHER if provider_cancel else USER, provider=USER if provider_cancel else OTHER
    )
    declared = event.start - timedelta(days=8)
    declaration = {
        "paid_contract_intent": "cancel_identified_contracts",
        "contract_ids": [booked.payment_id],
        "original_text": "Please cancel this identified booking.",
        "received_at": declared.isoformat(),
    }
    remote[USER] = canonical(USER, declaration=declaration)
    await delete_user_data(USER)
    assert await db.get(WebinarParticipant, webinar_id=event.id, user_id=booked.user_id) is None
    claims = await db.all(select(SettlementClaim))
    student_claim = next(c for c in claims if c.user_id == booked.user_id)
    assert student_claim.entitlement == "established" and student_claim.coins == 1337
    assert student_claim.basis["request_received_at"] == declared.isoformat()
    assert student_claim.basis["cancellation_declaration"]["contract_id"] == booked.payment_id
    assert "original_text" not in str(student_claim.basis)
    assert await db.all(select(RetainedEventRight)) == []


async def test_cleanup_before_data_erasure_preserves_unknown_performance(session, remote, mocker):
    event, booked = await booking(days=-1)
    remote[USER] = canonical(USER)
    remote[USER]["request"]["received_at"] = (event.start - timedelta(days=8)).isoformat()
    await clean_old_webinars.__wrapped__()
    assert await db.get(Webinar, id=event.id) is None
    rights = await db.all(select(RetainedEventRight))
    assert len(rights) == 1 and rights[0].state == "resolution_pending"
    claims = await db.all(select(SettlementClaim))
    assert len(claims) == 2 and all(c.entitlement == "pending_evidence" for c in claims)
    assert all(c.basis["cancellation_inferred_from_erasure"] is False for c in claims)
    from api.models import EventBenefit

    assert await db.all(select(EventBenefit)) == []
    ids = {c.id for c in claims}
    await delete_user_data(USER)
    assert {c.id for c in await db.all(select(SettlementClaim))} == ids
    assert len(await db.all(select(RetainedEventErasure))) == 1


async def test_old_erasure_does_not_withdraw_current_successor_observation(session, remote):
    event, booked = await booking()
    remote[USER] = canonical(USER)
    await delete_user_data(USER)
    right = await db.first(select(RetainedEventRight))
    # Explicit fixture of a delivered successor; this does not certify the pending adapter.
    right.current_subject = THIRD
    right.state = "active"
    await db.commit()
    await delete_user_data(USER)
    assert right.current_subject == THIRD and right.state == "active"
    assert len(await db.all(select(RetainedEventErasure))) == 1


async def test_unknown_original_receipt_preserves_right_without_false_acknowledgment(session, remote):
    event, booked = await booking()
    with pytest.raises(HTTPException) as failure:
        await delete_user_data(USER)
    assert failure.value.status_code == 503
    assert await db.get(WebinarParticipant, webinar_id=event.id, user_id=USER) is not None
    receipt = await db.get(commercial.CommercialErasureReceipt, subject=USER)
    assert receipt.canonical is None and receipt.erased_at is not None and receipt.acknowledged_at is None
    assert await db.all(select(SettlementClaim)) == []


async def test_delayed_cleanup_preserves_actual_earlier_cancellation(session, remote, mocker):
    event, booked = await booking(days=-1)
    declared = event.start - timedelta(days=8)
    remote[USER] = canonical(
        USER,
        declaration={
            "paid_contract_intent": "cancel_identified_contracts",
            "contract_ids": [booked.payment_id],
            "original_text": "Cancel the identified booking",
            "received_at": declared.isoformat(),
        },
    )
    await clean_old_webinars.__wrapped__()
    claims = await db.all(select(SettlementClaim))
    assert len(claims) == 1 and claims[0].user_id == USER and claims[0].entitlement == "established"
    assert claims[0].coins == 1337 and claims[0].basis["request_received_at"] == declared.isoformat()
    await delete_user_data(USER)
    assert len(await db.all(select(SettlementClaim))) == 1


async def test_cleanup_queries_current_recipient_and_keeps_original_financial_owner(session, remote):
    event, booked = await booking()
    remote[USER] = canonical(USER)
    await delete_user_data(USER)
    right = await db.first(select(RetainedEventRight))
    right.current_subject = THIRD
    right.state = "active"
    await db.commit()
    from api.models import BookingPayment
    from api.services import settlements

    batch = await settlements.new_batch("payout")
    payment = await db.get(BookingPayment, id=booked.payment_id)
    assert await commercial.cleanup_claims(payment, event, OTHER, batch.id) is False
    assert payment.user_id == USER and right.current_subject == THIRD
    assert await db.all(select(SettlementClaim)) == []


@pytest.mark.parametrize("kind", ["webinar", "coaching"])
@pytest.mark.parametrize("path", ["direct", "cleanup", "detached"])
async def test_provider_cancellation_exactly_at_start_has_same_return(session, remote, mocker, kind, path):
    from api.models import BookingPayment
    from api.models.slots import clean_old_slots

    if kind == "webinar":
        event, booked = await booking(student=OTHER, provider=USER, days=-1)
        payment_id = booked.payment_id
    else:
        event = _slot(str(uuid4()), USER, OTHER)
        event.start = utcnow() - timedelta(days=1)
        event.end = event.start + timedelta(hours=1)
        await db.add(event)
        payment_id = event.payment_id
    payment = await db.get(BookingPayment, id=payment_id)
    declared = event.start
    remote[USER] = canonical(
        USER,
        declaration={
            "paid_contract_intent": "cancel_identified_contracts",
            "contract_ids": [payment_id],
            "original_text": "Provider cancels this identified booking at its agreed start.",
            "received_at": declared.isoformat(),
        },
    )
    if path == "detached":
        await commercial.retain_event(payment, event, USER)
        await db.delete(event)
        await db.session.flush()
    if path == "cleanup":
        await (clean_old_webinars.__wrapped__() if kind == "webinar" else clean_old_slots.__wrapped__())
    else:
        await delete_user_data(USER)
    claims = await db.all(select(SettlementClaim))
    student = next(c for c in claims if c.user_id == OTHER)
    assert student.entitlement == "established"
    assert student.coins == payment.paid_coins
    assert student.basis["cancellation_declaration"]["received_at"] == declared.isoformat()


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        {},
        {"request": []},
        {"request": {"evidence": []}},
        {"request": {"evidence": {"declaration": {"contract_ids": "x"}}}},
    ],
)
def test_erasure_without_actual_declaration_never_implies_cancellation(malformed):
    assert retained_events.cancellation_declaration(malformed, "x") is None
