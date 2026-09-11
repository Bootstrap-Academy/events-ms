"""Owning local Event continuation; backend exact elections are stubbed."""

from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.database import db, filter_by, select
from api.endpoints.learning import existing_events
from api.models import BookingPayment, EventRightGrant, EventSubjectGuard, RetainedEventRight, WebinarParticipant
from api.services import retained_events
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data
from api.utils.utc import utcnow
from tests.payment_fixtures import paid_participant
from tests.services.test_retained_events import booking, canonical, remote
from tests.services.test_user_deletion import OTHER, THIRD, USER, _slot


async def preserved(remote, kind="webinar", role="participant", payout=None):
    provider, participant = (USER, OTHER) if role == "instructor" else (OTHER, USER)
    if kind == "webinar":
        event, booked = await booking(student=participant, provider=provider, days=0)
        payment_id = booked.payment_id
    else:
        event = _slot(str(uuid4()), provider, participant)
        event.start = utcnow()
        event.end = event.start + timedelta(hours=1)
        await db.add(event)
        payment_id = event.payment_id
    if payout is not None:
        (await db.get(BookingPayment, id=payment_id)).payout_coins = payout
    remote[USER] = canonical(USER)
    await delete_user_data(USER)
    right = await db.first(filter_by(RetainedEventRight, payment_id=payment_id, role=role))
    return event, right, payment_id


async def authorize(mocker, right, subject=THIRD):
    grant_id = str(uuid4())
    authority = {
        "id": grant_id,
        "source": "events",
        "purpose": "existing_event_continuation",
        "new_purchase": False,
        "successor": subject,
        "original_contract": right.id,
        "original_scope": await retained_events.get_original(USER, right.id),
        "claimant_authorization": {"source_subject": USER},
    }
    call = mocker.patch("api.services.retained_events.successor_authority", AsyncMock(return_value=authority))
    return grant_id, authority, call


@pytest.mark.parametrize("kind", ["webinar", "coaching"])
@pytest.mark.parametrize("role", ["participant", "instructor"])
async def test_existing_event_delivery_preserves_original_payment_period_and_role(session, remote, mocker, kind, role):
    event, right, payment_id = await preserved(remote, kind, role)
    payment = await db.get(BookingPayment, id=payment_id)
    before = (payment.user_id, payment.paid_coins, event.start, event.end)
    grant_id, _, _ = await authorize(mocker, right)
    result = await retained_events.deliver(USER, grant_id)
    await db.commit()
    assert result["state"] == "granted" and result["original_result"]["new_purchase"] is False
    assert (payment.user_id, payment.paid_coins, event.start, event.end) == before
    assert right.current_subject == THIRD
    view = await existing_events(THIRD, event.id)
    assert len(view) == 1 and view[0]["state"] == "available" and view[0]["role"] == role
    assert view[0]["link"] == (event.admin_link if role == "instructor" else event.link)
    assert await existing_events(str(uuid4()), event.id) == []
    assert await retained_events.deliver(USER, grant_id) == result
    assert (await export_user_data(USER)).retained_event_rights["grants"][0]["id"] == grant_id
    assert (await export_user_data(str(uuid4()))).retained_event_rights["grants"] == []


async def test_t2_withdraws_exact_grant_then_new_election_continues_same_seat(session, remote, mocker):
    event, right, payment_id = await preserved(remote)
    grant_id, _, call = await authorize(mocker, right)
    first = await retained_events.deliver(USER, grant_id)
    await db.commit()
    remote[THIRD] = canonical(THIRD)
    await delete_user_data(THIRD)
    call.reset_mock()
    old = await retained_events.deliver(USER, grant_id)
    assert old["state"] == "withdrawn" and old["original_result"] == first["original_result"]
    call.assert_not_awaited()
    assert await existing_events(THIRD, event.id) == []
    next_subject = str(uuid4())
    second_id, _, _ = await authorize(mocker, right, next_subject)
    assert (await retained_events.deliver(USER, second_id))["state"] == "granted"
    await db.commit()
    participants = await db.all(filter_by(WebinarParticipant, webinar_id=event.id))
    assert (
        len(participants) == 1 and participants[0].user_id == next_subject and participants[0].payment_id == payment_id
    )
    assert (await db.get(BookingPayment, id=payment_id)).user_id == USER
    assert len(await db.all(select(EventRightGrant))) == 2


async def test_changed_admission_and_erased_target_do_not_grant(session, remote, mocker):
    _, right, _ = await preserved(remote)
    grant_id, authority, call = await authorize(mocker, right)
    call.side_effect = [authority, None]
    with pytest.raises(HTTPException) as denied:
        await retained_events.deliver(USER, grant_id)
    assert denied.value.status_code == 409 and await db.all(select(EventRightGrant)) == []
    guard = await retained_events.lock_subject(THIRD)
    guard.deleted = True
    await db.commit()
    call.side_effect = None
    with pytest.raises(HTTPException) as erased:
        await retained_events.deliver(USER, grant_id)
    assert erased.value.status_code == 409
    assert (await db.get(EventSubjectGuard, subject=THIRD)).deleted is True and right.current_subject is None


async def test_existing_other_seat_is_not_merged(session, remote, mocker):
    event, right, _ = await preserved(remote)
    await db.add(paid_participant(webinar_id=event.id, user_id=THIRD, paid_coins=99))
    grant_id, _, _ = await authorize(mocker, right)
    with pytest.raises(HTTPException) as conflict:
        await retained_events.deliver(USER, grant_id)
    assert conflict.value.status_code == 409
    assert len(await db.all(filter_by(WebinarParticipant, webinar_id=event.id))) == 2
    assert await db.all(select(EventRightGrant)) == []


async def test_ended_period_requires_resolution_without_extension(session, remote, mocker):
    event, right, _ = await preserved(remote)
    event.end = utcnow() - timedelta(seconds=1)
    grant_id, _, _ = await authorize(mocker, right)
    with pytest.raises(HTTPException) as expired:
        await retained_events.deliver(USER, grant_id)
    assert expired.value.status_code == 409
    assert await db.all(select(EventRightGrant)) == [] and right.state == "preserved"


async def test_provider_continuation_covers_existing_participants_without_reopening_sales(session, remote, mocker):
    event, first = await booking(student=OTHER, provider=USER, days=0)
    second = paid_participant(webinar_id=event.id, user_id=str(uuid4()), paid_coins=77)
    await db.add(second)
    remote[USER] = canonical(USER)
    await delete_user_data(USER)
    rights = await db.all(filter_by(RetainedEventRight, source_subject=USER, event_id=event.id))
    assert len(rights) == 2
    grant_id, _, _ = await authorize(mocker, rights[0])
    result = await retained_events.deliver(USER, grant_id)
    await db.commit()
    assert set(result["original_result"]["affected_right_ids"]) == {r.id for r in rights}
    assert all(r.current_subject == THIRD and r.state == "active" for r in rights)
    assert event.creator == THIRD and event.closed_to_new_bookings is True
    assert len(await db.all(filter_by(WebinarParticipant, webinar_id=event.id))) == 2
    assert len(await existing_events(THIRD, event.id)) == 1
    remote[THIRD] = canonical(THIRD)
    await delete_user_data(THIRD)
    assert all(r.current_subject is None and r.state == "preserved" for r in rights)
    assert (await retained_events.deliver(USER, grant_id))["state"] == "withdrawn"
    assert await existing_events(THIRD, event.id) == []
    assert (await db.get(BookingPayment, id=first.payment_id)).user_id == OTHER


@pytest.mark.parametrize("kind", ["webinar", "coaching"])
async def test_cancel_succeeded_participation_preserves_original_financial_owner(session, remote, mocker, kind):
    from api.models import SettlementClaim
    from api.schemas.user import User
    from api.schemas.ordinary_cancellation import CancellationDeclaration, CancellationPreparation
    from api.services import ordinary_cancellations

    event, right, payment_id = await preserved(remote, kind)
    grant_id, _, _ = await authorize(mocker, right)
    await retained_events.deliver(USER, grant_id)
    await db.commit()
    mocker.patch("api.services.ordinary_cancellations.clear_cache", AsyncMock())
    mocker.patch("api.services.settlements.finish", AsyncMock())
    mocker.patch("api.services.ordinary_cancellations.utcnow", return_value=event.start - timedelta(days=8))
    user = User(id=THIRD, email_verified=True, admin=False)
    target = await ordinary_cancellations.prepare(user, event.id, CancellationPreparation(kind=kind))
    result = await ordinary_cancellations.receive(
        user,
        str(uuid4()),
        CancellationDeclaration(
            target_id=target["id"],
            cancel_selected_scope=True,
            original_text="I cancel this exact succeeded participation.",
        ),
    )
    assert result["state"] == "applied"
    claims = await db.all(select(SettlementClaim))
    assert len(claims) == 1 and claims[0].user_id == USER
    assert claims[0].coins == (await db.get(BookingPayment, id=payment_id)).paid_coins
    assert claims[0].entitlement == "established"
    assert await existing_events(THIRD, event.id) == []
    assert right.state == "cancelled" and right.current_subject is None
    assert (await db.get(EventRightGrant, id=grant_id)).state == "withdrawn"
    assert (await export_user_data(USER)).retained_event_rights["grants"][0]["id"] == grant_id


@pytest.mark.parametrize("payout", [None, 123])
async def test_cleanup_succeeded_host_retains_original_remuneration_owner(session, remote, mocker, payout):
    from api.models import SettlementClaim
    from api.models.webinars import clean_old_webinars

    event, right, payment_id = await preserved(remote, role="instructor", payout=payout)
    grant_id, _, _ = await authorize(mocker, right)
    await retained_events.deliver(USER, grant_id)
    await db.commit()
    mocker.patch("api.models.webinars.utcnow", return_value=event.end + timedelta(seconds=1))
    await clean_old_webinars.__wrapped__()
    claims = await db.all(select(SettlementClaim))
    assert len(claims) == 1 and claims[0].user_id == USER and claims[0].coins == payout
    assert (await db.get(BookingPayment, id=payment_id)).user_id == OTHER
    from api.models import EventBenefit

    assert {award.user_id for award in await db.all(select(EventBenefit))} == {OTHER, THIRD}
