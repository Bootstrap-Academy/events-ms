"""Endpoints related to 1-on-1 coachings"""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db, filter_by
from api.exceptions.auth import verified_responses
from api.exceptions.coaching import CannotBookOwnCoachingError, CoachingNotFoundError, NotEnoughCoinsError
from api.exceptions.skills import SkillRequirementsNotMetError
from api.models.slots import EventType
from api.schemas.coachings import Coaching, CoachingSlot, PublicCoaching, UpdateCoaching
from api.schemas.user import User
from api.services import shop
from api.services.auth import get_email, get_instructor
from api.services.skills import get_completed_skills, get_lecturers
from api.settings import settings
from api.utils.cache import clear_cache, redis_cached
from api.utils.email import BOOKED_COACHING
from api.utils.utc import utcnow


router = APIRouter()


@router.get("/coachings", dependencies=[require_verified_email], responses=verified_responses(list[Coaching]))
async def get_coachings(user: User = user_auth) -> Any:
    """
    Return a list of all coachings for an instructor.

    *Requirements:* **VERIFIED**
    """

    return [
        Coaching(skill_id=coaching.skill_id, price=coaching.price)
        async for coaching in await db.stream(filter_by(models.Coaching, user_id=user.id))
    ]


@router.get(
    "/coachings/{skill_id}", dependencies=[require_verified_email], responses=verified_responses(list[CoachingSlot])
)
@redis_cached("calendar", "skill_id")
async def get_slots(skill_id: str) -> Any:
    """
    Return a list of available times for a coaching session.

    *Requirements:* **VERIFIED**
    """

    out = []
    instructor_id: str
    for instructor_id in await get_lecturers({skill_id, settings.coaching_skill}):
        coaching = await db.get(models.Coaching, user_id=instructor_id, skill_id=skill_id)
        if not coaching:
            continue

        instructor = await get_instructor(instructor_id)
        if not instructor:
            continue

        slot: models.Slot
        async for slot in await db.stream(
            filter_by(models.Slot, user_id=instructor_id, booked_by=None).where(
                models.Slot.start >= utcnow() + timedelta(days=1)
            )
        ):
            out.append(
                CoachingSlot(
                    id=slot.id,
                    coaching=PublicCoaching(instructor=instructor, skill_id=skill_id, price=coaching.price),
                    start=slot.start.timestamp(),
                    end=slot.end.timestamp(),
                )
            )

    return out


@router.post(
    "/coachings/{skill_id}/{slot_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(CoachingSlot, CoachingNotFoundError, NotEnoughCoinsError, CannotBookOwnCoachingError),
)
async def book_coaching(skill_id: str, slot_id: str, user: User = user_auth) -> Any:
    """
    Book a coaching session.

    *Requirements:* **VERIFIED**
    """

    slot = await db.get(models.Slot, id=slot_id, booked_by=None)
    if not slot or slot.start - utcnow() < timedelta(days=1):
        raise CoachingNotFoundError

    if slot.user_id == user.id:
        raise CannotBookOwnCoachingError

    coaching = await db.get(models.Coaching, user_id=slot.user_id)
    if not coaching:
        raise CoachingNotFoundError

    instructor = await get_instructor(slot.user_id)
    if not instructor:
        raise CoachingNotFoundError

    if not await shop.spend_coins(user.id, coaching.price):
        raise NotEnoughCoinsError

    slot.book(user.id, EventType.COACHING, coaching.price, int(coaching.price * (1 - settings.event_fee)), skill_id)

    await clear_cache("calendar")

    if email := await get_email(user.id):
        await BOOKED_COACHING.send(
            email,
            instructor=instructor,
            datetime=slot.start.strftime("%d.%m.%Y %H:%M"),
            location=slot.meeting_link,
            coins=coaching.price,
        )

    return CoachingSlot(
        id=slot.id,
        coaching=PublicCoaching(instructor=instructor, skill_id=skill_id, price=coaching.price),
        start=slot.start.timestamp(),
        end=slot.end.timestamp(),
    )


@router.put(
    "/coachings/{skill_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(Coaching, SkillRequirementsNotMetError),
)
async def set_coaching(data: UpdateCoaching, skill_id: str, user: User = user_auth) -> Any:
    """
    Set up a coaching for a skill.

    *Requirements:* **VERIFIED**
    """

    if not user.admin and not {settings.coaching_skill, skill_id}.issubset(await get_completed_skills(user.id)):
        raise SkillRequirementsNotMetError

    coaching = await db.get(models.Coaching, user_id=user.id, skill_id=skill_id)
    if not coaching:
        await db.add(coaching := models.Coaching(user_id=user.id, skill_id=skill_id, price=data.price))
    else:
        coaching.price = data.price

    await clear_cache("calendar")

    return Coaching(skill_id=coaching.skill_id, price=coaching.price)


@router.delete(
    "/coachings/{skill_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(bool, CoachingNotFoundError),
)
async def delete_coaching(skill_id: str, user: User = user_auth) -> Any:
    """
    Delete a coaching for a skill.

    *Requirements:* **VERIFIED**
    """

    coaching = await db.get(models.Coaching, user_id=user.id, skill_id=skill_id)
    if not coaching:
        raise CoachingNotFoundError

    await db.delete(coaching)

    await clear_cache("calendar")

    return True
