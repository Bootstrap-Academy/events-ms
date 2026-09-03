from datetime import time, timedelta
from typing import Any
from unittest.mock import AsyncMock, call

import pytest
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db, select
from api.models import (
    CalendarToken,
    Coaching,
    EmergencyCancel,
    EventType,
    Exam,
    LecturerRating,
    Slot,
    Webinar,
    WebinarParticipant,
    WeeklySlot,
)
from api.services.user_deletion import USER_CACHE_PREFIXES, delete_user_data
from api.utils.utc import utcnow


USER = "40ab0e5c-b7ee-4a25-9d10-1eaf3c62d2bd"
OTHER = "9f4e2d17-9e2b-4b02-8c0f-3a8c07c5f4f0"
THIRD = "c1d2eb59-8b1a-4a2f-9c37-1b8e5d7f60a3"


def _webinar(webinar_id: str, creator: str) -> Webinar:
    return Webinar(
        id=webinar_id,
        skill_id="test",
        creator=creator,
        creation_date=utcnow(),
        name="test webinar",
        description="test description",
        admin_link="https://meet.jit.si/admin",
        link="https://meet.jit.si/link",
        start=utcnow() + timedelta(days=1),
        end=utcnow() + timedelta(days=1, hours=1),
        max_participants=42,
        price=1337,
    )


def _slot(slot_id: str, user_id: str, booked_by: str | None, weekly_slot_id: str | None = None) -> Slot:
    return Slot(
        id=slot_id,
        user_id=user_id,
        start=utcnow() + timedelta(days=1),
        end=utcnow() + timedelta(days=1, hours=1),
        booked_by=booked_by,
        event_type=EventType.COACHING if booked_by else None,
        student_coins=42 if booked_by else None,
        instructor_coins=21 if booked_by else None,
        skill_id="test" if booked_by else None,
        admin_link="https://meet.jit.si/admin" if booked_by else None,
        link="https://meet.jit.si/link" if booked_by else None,
        weekly_slot_id=weekly_slot_id,
    )


def _weekly_slot(weekly_slot_id: str, user_id: str) -> WeeklySlot:
    return WeeklySlot(
        id=weekly_slot_id, user_id=user_id, weekday=3, start=time(10, 0), end=time(11, 0), last_slot=utcnow()
    )


def _rating(rating_id: str, lecturer_id: str, participant_id: str | None) -> LecturerRating:
    return LecturerRating(
        id=rating_id,
        lecturer_id=lecturer_id,
        participant_id=participant_id,
        skill_id="test",
        webinar_timestamp=utcnow(),
        webinar_name="test webinar",
        rating=None if participant_id else 5,
    )


@pytest.fixture(autouse=True)
def clear_cache_patch(mocker: MockerFixture) -> AsyncMock:
    return mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())


@pytest.fixture
async def data(session: AsyncSession) -> None:
    # webinars, including the ones the users have booked
    await db.add(_webinar("webinar-user", USER))
    await db.add(_webinar("webinar-other", OTHER))
    await db.add(_webinar("webinar-third", THIRD))
    await db.add(WebinarParticipant(webinar_id="webinar-user", user_id=OTHER))
    await db.add(WebinarParticipant(webinar_id="webinar-other", user_id=USER))
    await db.add(WebinarParticipant(webinar_id="webinar-third", user_id=USER))
    await db.add(WebinarParticipant(webinar_id="webinar-third", user_id=OTHER))

    # slots the users offer as lecturers, one of them booked by the respective other user
    await db.add(_weekly_slot("weekly-user", USER))
    await db.add(_weekly_slot("weekly-other", OTHER))
    await db.add(_slot("slot-user", USER, None, "weekly-user"))
    await db.add(_slot("slot-user-booked", USER, OTHER))
    await db.add(_slot("slot-other", OTHER, None, "weekly-other"))
    await db.add(_slot("slot-other-booked", OTHER, USER))

    await db.add(Coaching(user_id=USER, skill_id="test", price=42))
    await db.add(Exam(user_id=USER, skill_id="test"))
    await db.add(EmergencyCancel(user_id=USER))

    await db.add(_rating("rating-lecturer", USER, None))
    await db.add(_rating("rating-participant", OTHER, USER))
    await db.add(_rating("rating-other", OTHER, OTHER))

    await CalendarToken.get_or_create(USER)
    await CalendarToken.get_or_create(OTHER)


async def _all(cls: Any) -> list[Any]:
    return await db.all(select(cls))


async def test__delete_user_data__deletes_everything(data: None) -> None:
    await delete_user_data(USER)

    assert sorted(w.id for w in await _all(Webinar)) == ["webinar-other", "webinar-third"]
    assert [(p.webinar_id, p.user_id) for p in await _all(WebinarParticipant)] == [("webinar-third", OTHER)]
    assert sorted(s.id for s in await _all(Slot)) == ["slot-other", "slot-other-booked"]
    assert [w.id for w in await _all(WeeklySlot)] == ["weekly-other"]
    assert [c.user_id for c in await _all(Coaching)] == []
    assert [e.user_id for e in await _all(Exam)] == []
    assert [e.user_id for e in await _all(EmergencyCancel)] == []
    assert [r.id for r in await _all(LecturerRating)] == ["rating-other"]
    assert [t.user_id for t in await _all(CalendarToken)] == [OTHER]


async def test__delete_user_data__frees_booked_slots(data: None) -> None:
    await delete_user_data(USER)

    slot = await db.get(Slot, id="slot-other-booked")
    assert slot is not None
    assert slot.user_id == OTHER
    assert slot.booked_by is None
    assert slot.event_type is None
    assert slot.student_coins is None
    assert slot.instructor_coins is None
    assert slot.skill_id is None
    assert slot.admin_link is None
    assert slot.link is None


async def test__delete_user_data__keeps_other_users(data: None) -> None:
    await delete_user_data(OTHER)

    assert sorted(w.id for w in await _all(Webinar)) == ["webinar-third", "webinar-user"]
    assert [(p.webinar_id, p.user_id) for p in await _all(WebinarParticipant)] == [("webinar-third", USER)]
    assert sorted(s.id for s in await _all(Slot)) == ["slot-user", "slot-user-booked"]
    assert [w.id for w in await _all(WeeklySlot)] == ["weekly-user"]
    assert [c.user_id for c in await _all(Coaching)] == [USER]
    assert [e.user_id for e in await _all(Exam)] == [USER]
    assert [e.user_id for e in await _all(EmergencyCancel)] == [USER]
    assert [r.id for r in await _all(LecturerRating)] == ["rating-lecturer"]
    assert [t.user_id for t in await _all(CalendarToken)] == [USER]

    slot = await db.get(Slot, id="slot-user-booked")
    assert slot is not None and slot.booked_by is None


async def test__delete_user_data__unknown_user(data: None) -> None:
    await delete_user_data("cb3b0d6e-8e1b-4b7c-9d64-c7a1a5a1e6a5")

    assert len(await _all(Webinar)) == 3
    assert len(await _all(WebinarParticipant)) == 4
    assert len(await _all(Slot)) == 4
    assert len(await _all(WeeklySlot)) == 2
    assert len(await _all(Coaching)) == 1
    assert len(await _all(Exam)) == 1
    assert len(await _all(EmergencyCancel)) == 1
    assert len(await _all(LecturerRating)) == 3
    assert len(await _all(CalendarToken)) == 2


async def test__delete_user_data__idempotent(data: None) -> None:
    await delete_user_data(USER)
    await delete_user_data(USER)

    assert sorted(w.id for w in await _all(Webinar)) == ["webinar-other", "webinar-third"]
    assert sorted(s.id for s in await _all(Slot)) == ["slot-other", "slot-other-booked"]


async def test__delete_user_data__clears_cache(data: None, clear_cache_patch: AsyncMock) -> None:
    await delete_user_data(USER)

    assert clear_cache_patch.call_args_list == [call(prefix) for prefix in USER_CACHE_PREFIXES]
