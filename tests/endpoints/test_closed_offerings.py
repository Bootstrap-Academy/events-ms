"""Stale clients cannot restart the retired event product or its side effects."""

from datetime import time, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db, select
from api.models import CalendarToken, Coaching, Slot, WeeklySlot
from api.models.slots import clean_old_slots
from api.schemas.user import User
from api.services.user_export import export_user_data
from api.utils.utc import utcnow
from tests.required import required, unwrapped


USER = "40ab0e5c-b7ee-4a25-9d10-1eaf3c62d2bd"
EVENT = "9f4e2d17-9e2b-4b02-8c0f-3a8c07c5f4f0"


@pytest.mark.parametrize(
    "method,path,code",
    [
        ("POST", "/webinars", "EventOfferingClosed"),
        ("PATCH", f"/webinars/{EVENT}", "EventOfferingClosed"),
        ("POST", f"/webinars/{EVENT}/offer", "EventOfferingClosed"),
        ("POST", f"/webinars/{EVENT}/participants", "EventOfferingClosed"),
        ("GET", "/coachings", "EventOfferingClosed"),
        ("PUT", "/coachings/python", "EventOfferingClosed"),
        ("POST", f"/coachings/python/{EVENT}/offer", "EventOfferingClosed"),
        ("POST", f"/coachings/python/{EVENT}", "EventOfferingClosed"),
        ("GET", "/slots/me", "EventOfferingClosed"),
        ("POST", "/slots/me", "EventOfferingClosed"),
        ("GET", "/slots/me/weekly", "EventOfferingClosed"),
        ("POST", "/slots/me/weekly", "EventOfferingClosed"),
        ("GET", "/unrated", "EventOfferingClosed"),
        ("POST", f"/rate/{EVENT}", "EventOfferingClosed"),
        ("GET", "/calendar", "EventCalendarClosed"),
        ("POST", "/calendar/token/rotate", "EventCalendarClosed"),
        ("GET", "/calendar/synthetic-calendar-token/academy.ics", "EventCalendarClosed"),
        ("GET", "/learning/calendar", "EventCalendarClosed"),
        ("POST", f"/learning/webinars/{EVENT}/offer", "EventOfferingClosed"),
        ("POST", f"/learning/webinars/{EVENT}/participants", "EventOfferingClosed"),
        ("POST", f"/learning/coachings/python/{EVENT}/offer", "EventOfferingClosed"),
        ("POST", f"/learning/coachings/python/{EVENT}", "EventOfferingClosed"),
    ],
)
async def test_closed_routes_stop_before_auth_data_payment_and_mail(
    client: AsyncClient, mocker: MockerFixture, method: str, path: str, code: str
) -> None:
    forbidden = [
        "api.auth.decode_jwt",
        "api.endpoints.learning.learning_subject",
        "api.services.booking_contracts.offer",
        "api.services.booking_contracts.prepare",
        "api.services.booking_payments.reserve",
        "api.utils.email.send_email",
        "api.endpoints.ratings.send_email",
    ]
    calls = [mocker.patch(name, side_effect=AssertionError("Retired route reached a dependency")) for name in forbidden]
    data_calls = [
        mocker.patch.object(db, name, side_effect=AssertionError("Retired route touched stored data"))
        for name in ("get", "first", "all", "add", "delete", "stream", "commit")
    ]
    for headers in ({}, {"Authorization": "Bearer old-client", "x-learning-key": "synthetic" * 8}):
        response = await client.request(method, path, headers=headers, json={} if method != "GET" else None)
        assert response.status_code == 410, response.text
        assert response.json()["detail"]["code"] == code
    for call in [*calls, *data_calls]:
        call.assert_not_called()


async def test_cleanup_preserves_future_slots_and_rules_but_does_not_generate_more(
    session: AsyncSession, mocker: MockerFixture
) -> None:
    last_slot = utcnow() - timedelta(days=1)
    rule = await db.add(
        WeeklySlot(id=str(uuid4()), user_id=USER, weekday=0, start=time(10), end=time(11), last_slot=last_slot)
    )
    future = await Slot.create(USER, utcnow() + timedelta(days=2), utcnow() + timedelta(days=2, hours=1))
    future.weekly_slot_id = rule.id
    await db.add(Coaching(user_id=USER, skill_id="python", price=42))
    token = (await CalendarToken.get_or_create(USER)).token
    await db.commit()
    finish = mocker.patch("api.services.settlements.finish", AsyncMock())
    generated = mocker.patch.object(WeeklySlot, "create_slots", AsyncMock(side_effect=AssertionError("New slots")))

    await unwrapped(clean_old_slots)()
    await db.commit()

    generated.assert_not_awaited()
    assert [row.id for row in await db.all(select(Slot))] == [future.id]
    assert required(await db.get(WeeklySlot, id=rule.id)).last_slot == last_slot
    assert await CalendarToken.get_user_id(token) == USER
    exported = await export_user_data(USER)
    assert [row.id for row in exported.weekly_slots] == [rule.id]
    assert exported.coachings[0].price == 42
    finish.assert_awaited_once()


async def test_existing_webinar_read_keeps_the_booked_access_without_a_new_booking_action(
    session: AsyncSession, mocker: MockerFixture
) -> None:
    from api.endpoints.webinars import get_webinar_by_id
    from tests.endpoints.test_webinars import _webinar
    from tests.payment_fixtures import paid_participant

    webinar = await db.add(_webinar())
    participant = await db.add(paid_participant(webinar_id=webinar.id, user_id=USER, paid_coins=42))
    webinar.participants.append(participant)
    await db.commit()
    serialized: dict[str, Any] = {}

    async def render(link: bool, admin: bool, booked: bool, bookable: bool) -> dict[str, bool]:
        serialized.update(link=link, admin=admin, booked=booked, bookable=bookable)
        return serialized

    mocker.patch.object(webinar, "serialize", side_effect=render)
    await get_webinar_by_id(webinar, User(id=USER, email_verified=True, admin=False))
    assert serialized["booked"] is True and serialized["bookable"] is False
    await get_webinar_by_id(webinar, User(id=str(uuid4()), email_verified=True, admin=False))
    assert serialized["booked"] is False and serialized["bookable"] is False
