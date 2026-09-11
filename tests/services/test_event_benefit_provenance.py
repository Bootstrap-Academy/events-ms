"""Legacy effects remain unknown; only explicit new producer provenance issues XP."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from api.database import db, db_context, filter_by, select
from api.models import BookingPayment, EventBenefit
from api.models.webinars import clean_old_webinars
from api.services import benefits, booking_payments
from api.services.user_export import export_user_data
from tests.services.test_retained_events import booking, remote
from tests.services.test_user_deletion import OTHER, USER


async def test_legacy_cleanup_retains_unknown_obligation_without_issuing_new_keyed_xp(session, remote, mocker):
    event, booked = await booking(days=-1)
    original = dict((await db.get(BookingPayment, id=booked.payment_id)).original)
    await clean_old_webinars.__wrapped__()
    await db.commit()
    rows = await db.all(select(EventBenefit))
    assert len(rows) == 2 and all(r.state == "review" and r.attempts == 0 for r in rows)
    assert all(r.receipt["previous_effect"] == "unknown" and r.receipt["entitlement_forfeited"] is False for r in rows)
    assert all(r.original["issuance_protocol"] is None for r in rows)
    target = mocker.patch("api.services.skills.apply_xp_benefit", AsyncMock())
    await benefits.recover()
    target.assert_not_awaited()
    async with db_context():
        assert len((await export_user_data(USER)).event_benefits["earnings"]) == 1
        assert len((await export_user_data(OTHER)).event_benefits["earnings"]) == 1
        assert (await db.get(BookingPayment, id=booked.payment_id)).xp_delivery_protocol is None


async def test_new_booking_in_legacy_session_does_not_authorize_a_second_host_award(session, remote, mocker):
    event, booked = await booking(days=-1)
    (await db.get(BookingPayment, id=booked.payment_id)).xp_delivery_protocol = 1
    await clean_old_webinars.__wrapped__()
    await db.commit()
    rows = await db.all(select(EventBenefit))
    host = next(r for r in rows if r.role == "instructor")
    student = next(r for r in rows if r.role == "participant")
    assert host.state == "review" and student.state == "pending"
    assert host.original["new_booking_protocol"] == 1 and host.original["new_session_protocol"] is None
    calls = []

    async def receiver(operation, request):
        calls.append(operation)
        return {"state": "applied", "applied": True, "operation_id": operation, "request": request}

    mocker.patch("api.services.skills.apply_xp_benefit", side_effect=receiver)
    await benefits.recover()
    assert calls == [student.id]


async def test_unproven_older_pending_record_is_not_automatic_repair_authority(session, remote, mocker):
    event, booked = await booking(days=-1)
    await clean_old_webinars.__wrapped__()
    await db.commit()
    row = (await db.all(select(EventBenefit)))[0]
    row.original = {"source": "older_unproven_record"}
    row.state = "uncertain"
    await db.commit()
    target = mocker.patch("api.services.skills.apply_xp_benefit", AsyncMock())
    await benefits.recover()
    target.assert_not_awaited()
    async with db_context():
        current = await db.get(EventBenefit, id=row.id)
        assert current.state == "review" and current.receipt["previous_effect"] == "unknown"


async def test_actual_new_booking_reserve_writes_separate_prospective_provenance(session):
    row = await booking_payments.reserve("webinar", str(uuid4()), USER, 100, "Synthetic new booking", False)
    assert row.xp_delivery_protocol == 1 and "xp_delivery_protocol" not in row.original
    assert row.paid_coins is None and row.state == "pending"


async def test_actual_new_webinar_creation_marks_host_session_in_owning_transaction(session, mocker):
    from api.endpoints.webinars import create_webinar
    from api.models import Webinar
    from api.schemas.user import User
    from api.schemas.webinars import CreateWebinar
    from api.utils.utc import utcnow

    mocker.patch("api.endpoints.webinars.clear_cache", AsyncMock())
    mocker.patch.object(Webinar, "serialize", AsyncMock(return_value={"created": True}))
    data = CreateWebinar(
        skill_id="synthetic",
        name="New session",
        description="Synthetic",
        admin_link="https://example.invalid/admin",
        link="https://example.invalid/meeting",
        start=int(utcnow().timestamp()) + 3600,
        duration=60,
        max_participants=4,
        price=100,
    )
    await create_webinar(data, User(id=OTHER, email_verified=True, admin=True))
    rows = await db.all(select(Webinar))
    assert len(rows) == 1 and rows[0].xp_delivery_protocol == 1
    await db.session.rollback()
    assert await db.all(select(Webinar)) == []
