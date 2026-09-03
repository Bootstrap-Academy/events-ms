from sqlalchemy import or_

from api.database import db, filter_by, select
from api.logger import get_logger
from api.models import (
    CalendarToken,
    Coaching,
    EmergencyCancel,
    Exam,
    LecturerRating,
    Slot,
    Webinar,
    WebinarParticipant,
    WeeklySlot,
)
from api.utils.cache import clear_cache


logger = get_logger(__name__)

# prefixes of cached values which are keyed on a user id
USER_CACHE_PREFIXES = ["user", "user_skills", "calendar", "lecturer_rating"]


async def delete_user_data(user_id: str) -> None:
    """
    Delete everything that belongs to a user from the database.

    Content the user has created (webinars, coachings and the slots they offer as a lecturer) is deleted, whereas
    bookings of events that are owned by somebody else are cancelled. Has to be called inside a database context so
    that all deletions happen in a single transaction.
    """

    counts = dict.fromkeys(
        [
            Webinar.__tablename__,
            WebinarParticipant.__tablename__,
            Slot.__tablename__,
            WeeklySlot.__tablename__,
            Coaching.__tablename__,
            Exam.__tablename__,
            EmergencyCancel.__tablename__,
            LecturerRating.__tablename__,
            CalendarToken.__tablename__,
        ],
        0,
    )
    cancelled_bookings = 0

    # webinars created by the user, including the participants of these webinars
    webinar: Webinar
    async for webinar in await db.stream(filter_by(Webinar, creator=user_id)):
        counts[WebinarParticipant.__tablename__] += len(webinar.participants)
        counts[Webinar.__tablename__] += 1
        await db.delete(webinar)

    # webinars of other lecturers the user has booked
    participant: WebinarParticipant
    async for participant in await db.stream(filter_by(WebinarParticipant, user_id=user_id)):
        counts[WebinarParticipant.__tablename__] += 1
        await db.delete(participant)

    # slots the user offers as a lecturer
    slot: Slot
    async for slot in await db.stream(filter_by(Slot, user_id=user_id)):
        counts[Slot.__tablename__] += 1
        await db.delete(slot)

    # slots of other lecturers the user has booked are freed instead of being deleted
    async for slot in await db.stream(filter_by(Slot, booked_by=user_id)):
        cancelled_bookings += 1
        slot.cancel()

    weekly_slot: WeeklySlot
    async for weekly_slot in await db.stream(filter_by(WeeklySlot, user_id=user_id)):
        counts[WeeklySlot.__tablename__] += 1
        await db.delete(weekly_slot)

    coaching: Coaching
    async for coaching in await db.stream(filter_by(Coaching, user_id=user_id)):
        counts[Coaching.__tablename__] += 1
        await db.delete(coaching)

    exam: Exam
    async for exam in await db.stream(filter_by(Exam, user_id=user_id)):
        counts[Exam.__tablename__] += 1
        await db.delete(exam)

    emergency_cancel: EmergencyCancel
    async for emergency_cancel in await db.stream(filter_by(EmergencyCancel, user_id=user_id)):
        counts[EmergencyCancel.__tablename__] += 1
        await db.delete(emergency_cancel)

    # ratings the user has received as a lecturer as well as ratings they have not submitted yet
    rating: LecturerRating
    async for rating in await db.stream(
        select(LecturerRating).where(
            or_(LecturerRating.lecturer_id == user_id, LecturerRating.participant_id == user_id)
        )
    ):
        counts[LecturerRating.__tablename__] += 1
        await db.delete(rating)

    # the token of the ics calendar feed, which stops working as soon as it is deleted
    calendar_token: CalendarToken
    async for calendar_token in await db.stream(filter_by(CalendarToken, user_id=user_id)):
        counts[CalendarToken.__tablename__] += 1
        await db.delete(calendar_token)

    for prefix in USER_CACHE_PREFIXES:
        await clear_cache(prefix)

    logger.info(
        "deleted user data (%s, cancelled bookings: %s)",
        ", ".join(f"{table}: {cnt}" for table, cnt in counts.items()),
        cancelled_bookings,
    )
