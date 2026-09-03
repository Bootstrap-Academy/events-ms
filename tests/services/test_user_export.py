from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db
from api.models import (
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
from api.services.user_export import export_user_data


USER = "40ab0e5c-b7ee-4a25-9d10-1eaf3c62d2bd"
OTHER = "9f4e2d17-9e2b-4b02-8c0f-3a8c07c5f4f0"
START = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)


def _webinar(webinar_id: str, creator: str) -> Webinar:
    return Webinar(
        id=webinar_id,
        skill_id="test",
        creator=creator,
        creation_date=START - timedelta(days=1),
        name=f"webinar {webinar_id}",
        description="test description",
        admin_link="https://meet.jit.si/admin",
        link="https://meet.jit.si/link",
        start=START,
        end=START + timedelta(hours=1),
        max_participants=42,
        price=1337,
    )


def _slot(slot_id: str, user_id: str, booked_by: str | None) -> Slot:
    return Slot(
        id=slot_id,
        user_id=user_id,
        start=START,
        end=START + timedelta(hours=1),
        booked_by=booked_by,
        event_type=EventType.COACHING if booked_by else None,
        student_coins=42 if booked_by else None,
        instructor_coins=21 if booked_by else None,
        skill_id="test" if booked_by else None,
        admin_link="https://meet.jit.si/admin" if booked_by else None,
        link="https://meet.jit.si/link" if booked_by else None,
        weekly_slot_id=None,
    )


def _rating(rating_id: str, lecturer_id: str, participant_id: str | None) -> LecturerRating:
    return LecturerRating(
        id=rating_id,
        lecturer_id=lecturer_id,
        participant_id=participant_id,
        skill_id="test",
        webinar_timestamp=START,
        webinar_name="test webinar" if participant_id else None,
        rating=None if participant_id else 5,
    )


@pytest.fixture
async def data(session: AsyncSession) -> None:
    await db.add(_webinar("webinar-user", USER))
    await db.add(_webinar("webinar-other", OTHER))
    await db.add(WebinarParticipant(webinar_id="webinar-user", user_id=OTHER))
    await db.add(WebinarParticipant(webinar_id="webinar-other", user_id=USER))

    await db.add(WeeklySlot(id="weekly-user", user_id=USER, weekday=3, start=time(10), end=time(11), last_slot=START))
    await db.add(WeeklySlot(id="weekly-other", user_id=OTHER, weekday=4, start=time(12), end=time(13), last_slot=START))
    await db.add(_slot("slot-user", USER, None))
    await db.add(_slot("slot-other-booked", OTHER, USER))

    await db.add(Coaching(user_id=USER, skill_id="test", price=42))
    await db.add(Coaching(user_id=OTHER, skill_id="test", price=43))
    await db.add(Exam(user_id=USER, skill_id="test"))
    await db.add(EmergencyCancel(user_id=USER))

    await db.add(_rating("rating-lecturer", USER, None))
    await db.add(_rating("rating-participant", OTHER, USER))
    await db.add(_rating("rating-other", OTHER, OTHER))


async def test__export_user_data(data: None) -> None:
    export = await export_user_data(USER)

    assert [webinar.id for webinar in export.webinars] == ["webinar-user"]
    assert export.webinars[0].participants == 1
    assert [p.webinar_id for p in export.webinar_participations] == ["webinar-other"]
    assert export.webinar_participations[0].name == "webinar webinar-other"
    assert [slot.id for slot in export.slots_offered] == ["slot-user"]
    assert [slot.id for slot in export.slots_booked] == ["slot-other-booked"]
    assert export.slots_booked[0].event_type == "coaching"
    assert [weekly_slot.id for weekly_slot in export.weekly_slots] == ["weekly-user"]
    assert [coaching.price for coaching in export.coachings] == [42]
    assert [exam.skill_id for exam in export.exams] == ["test"]
    assert export.emergency_cancel is True
    assert [rating.id for rating in export.lecturer_ratings_received] == ["rating-lecturer"]
    assert [rating.id for rating in export.lecturer_ratings_requested] == ["rating-participant"]


async def test__export_user_data__contains_no_other_user_ids(data: None) -> None:
    export = await export_user_data(USER)

    assert OTHER not in export.json()


async def test__export_user_data__unknown_user(data: None) -> None:
    export = await export_user_data("cb3b0d6e-8e1b-4b7c-9d64-c7a1a5a1e6a5")

    assert export == export.__class__(
        webinars=[],
        webinar_participations=[],
        slots_offered=[],
        slots_booked=[],
        weekly_slots=[],
        coachings=[],
        exams=[],
        emergency_cancel=False,
        lecturer_ratings_received=[],
        lecturer_ratings_requested=[],
    )
