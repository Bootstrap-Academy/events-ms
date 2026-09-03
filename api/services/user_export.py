from api import models
from api.database import db, filter_by
from api.schemas import user_export as schemas


def _slot(slot: models.Slot) -> schemas.Slot:
    """Serialize a slot without the user id of the other party."""

    return schemas.Slot(
        id=slot.id,
        start=slot.start,
        end=slot.end,
        booked=slot.booked,
        event_type=slot.event_type.value if slot.event_type else None,
        skill_id=slot.skill_id,
        student_coins=slot.student_coins,
        instructor_coins=slot.instructor_coins,
        link=slot.link,
    )


def _rating(rating: models.LecturerRating) -> schemas.LecturerRating:
    """Serialize a rating without the user ids of the lecturer and the participant."""

    return schemas.LecturerRating(
        id=rating.id,
        skill_id=rating.skill_id,
        webinar_timestamp=rating.webinar_timestamp,
        webinar_name=rating.webinar_name,
        rating=rating.rating,
    )


async def export_user_data(user_id: str) -> schemas.UserDataExport:
    """
    Collect everything this service stores about a user.

    Only rows that belong to the given user are read and the user ids of other people are left out, so the export
    never contains anybody else's data. Has to be called inside a database context.
    """

    return schemas.UserDataExport(
        webinars=[
            schemas.Webinar(
                id=webinar.id,
                skill_id=webinar.skill_id,
                creation_date=webinar.creation_date,
                name=webinar.name,
                description=webinar.description,
                link=webinar.link,
                admin_link=webinar.admin_link,
                start=webinar.start,
                end=webinar.end,
                max_participants=webinar.max_participants,
                price=webinar.price,
                participants=len(webinar.participants),
            )
            async for webinar in await db.stream(filter_by(models.Webinar, creator=user_id))
        ],
        webinar_participations=[
            schemas.WebinarParticipation(
                webinar_id=participation.webinar_id,
                skill_id=participation.webinar.skill_id,
                name=participation.webinar.name,
                start=participation.webinar.start,
            )
            async for participation in await db.stream(filter_by(models.WebinarParticipant, user_id=user_id))
        ],
        slots_offered=[_slot(slot) async for slot in await db.stream(filter_by(models.Slot, user_id=user_id))],
        slots_booked=[_slot(slot) async for slot in await db.stream(filter_by(models.Slot, booked_by=user_id))],
        weekly_slots=[
            schemas.WeeklySlot(
                id=weekly_slot.id, weekday=weekly_slot.weekday, start=weekly_slot.start, end=weekly_slot.end
            )
            async for weekly_slot in await db.stream(filter_by(models.WeeklySlot, user_id=user_id))
        ],
        coachings=[
            schemas.Coaching(skill_id=coaching.skill_id, price=coaching.price)
            async for coaching in await db.stream(filter_by(models.Coaching, user_id=user_id))
        ],
        exams=[
            schemas.Exam(skill_id=exam.skill_id)
            async for exam in await db.stream(filter_by(models.Exam, user_id=user_id))
        ],
        emergency_cancel=await models.EmergencyCancel.exists(user_id),
        lecturer_ratings_received=[
            _rating(rating) async for rating in await db.stream(filter_by(models.LecturerRating, lecturer_id=user_id))
        ],
        lecturer_ratings_requested=[
            _rating(rating)
            async for rating in await db.stream(filter_by(models.LecturerRating, participant_id=user_id))
        ],
    )
