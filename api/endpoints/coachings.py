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
from api.schemas import calendar
from api.schemas.coachings import Coaching, UpdateCoaching
from api.schemas.user import User
from api.services import shop
from api.services.auth import get_email, get_userinfo
from api.services.skills import get_skill_levels
from api.settings import settings
from api.utils.cache import clear_cache
from api.utils.email import BOOKED_COACHING
from api.utils.utc import utcnow


router = APIRouter()


@router.post(
    "/coachings/{skill_id}/{slot_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(
        calendar.Coaching, CoachingNotFoundError, NotEnoughCoinsError, CannotBookOwnCoachingError
    ),
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

    instructor = await get_userinfo(slot.user_id)
    if not instructor:
        raise CoachingNotFoundError

    if not await models.EmergencyCancel.delete(slot.user_id) and not await shop.spend_coins(user.id, coaching.price):
        raise NotEnoughCoinsError

    slot.book(user.id, EventType.COACHING, coaching.price, int(coaching.price * (1 - settings.event_fee)), skill_id)

    await clear_cache("calendar")

    if email := await get_email(user.id):
        await BOOKED_COACHING.send(
            email,
            instructor=instructor.display_name,
            date=slot.start.strftime("%d.%m.%Y"),
            time=slot.start.strftime("%H:%M"),
            link=slot.link,
            coins=coaching.price,
        )

    return calendar.Coaching(
        id=slot.id,
        title=None,
        description=None,
        skill_id=slot.skill_id,
        start=slot.start.timestamp(),
        duration=(slot.end - slot.start).total_seconds() // 60,
        price=slot.student_coins,
        admin_link=None,
        link=slot.link,
        instructor=await get_userinfo(slot.user_id),
        instructor_rating=await models.LecturerRating.get_rating(slot.user_id, slot.skill_id),
        booked=True,
        bookable=False,
        student=await get_userinfo(slot.booked_by),
    )


@router.get("/coachings", dependencies=[require_verified_email], responses=verified_responses(list[Coaching]))
async def get_coachings(user: User = user_auth) -> Any:
    """
    Return a list of all coaching configurations for an instructor.

    *Requirements:* **VERIFIED**
    """

    return [
        Coaching(skill_id=coaching.skill_id, price=coaching.price)
        async for coaching in await db.stream(filter_by(models.Coaching, user_id=user.id))
    ]


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

    if not user.admin and not (await get_skill_levels(user.id)).get(skill_id, 0) < settings.coaching_level:
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
