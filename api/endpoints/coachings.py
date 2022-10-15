"""Endpoints related to 1-on-1 coachings"""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db, filter_by
from api.exceptions.auth import verified_responses
from api.exceptions.calendly import CalendlyNotConfiguredError
from api.exceptions.coaching import CoachingNotFoundError, NotEnoughCoinsError
from api.exceptions.skills import SkillRequirementsNotMetError
from api.schemas.coachings import Coaching, CoachingSlot, UpdateCoaching
from api.schemas.user import User
from api.services import calendly, shop
from api.services.skills import get_completed_skills, get_lecturers
from api.settings import settings


router = APIRouter()


@router.get("/coachings", dependencies=[require_verified_email], responses=verified_responses(list[Coaching]))
async def get_coachings(user: User = user_auth) -> Any:
    """
    Return a list of all coachings for an instructor.

    *Requirements:* **VERIFIED**
    """

    return [
        Coaching(instructor=coaching.user_id, skill_id=coaching.skill_id, price=coaching.price)
        async for coaching in await db.stream(filter_by(models.Coaching, user_id=user.id))
    ]


@router.get(
    "/coachings/{skill_id}", dependencies=[require_verified_email], responses=verified_responses(list[CoachingSlot])
)
async def get_available_times(skill_id: str) -> Any:
    """
    Return a list of available times for a coaching session.

    *Requirements:* **VERIFIED**
    """

    out = []
    for instructor in await get_lecturers({skill_id, settings.coaching_skill}):
        link = await db.get(models.CalendlyLink, user_id=instructor)
        if not link:
            continue

        coaching = await db.get(models.Coaching, user_id=instructor, skill_id=skill_id)
        if not coaching:
            continue

        event_type = await calendly.fetch_event_type(link.api_token, link.uri)
        if not event_type:
            continue

        for slot in await calendly.fetch_available_slots(link.api_token, link.uri) or []:
            out.append(
                CoachingSlot(
                    coaching=Coaching(instructor=instructor, skill_id=skill_id, price=coaching.price),
                    start=slot,
                    end=slot + timedelta(minutes=event_type.duration),
                )
            )

    return out


@router.post(
    "/coachings/{skill_id}/{instructor}",
    dependencies=[require_verified_email],
    responses=verified_responses(str, CoachingNotFoundError, NotEnoughCoinsError),
)
async def book_coaching(skill_id: str, instructor: str, user: User = user_auth) -> Any:
    """
    Book a coaching session.

    *Requirements:* **VERIFIED**
    """

    coaching = await db.get(models.Coaching, user_id=instructor, skill_id=skill_id)
    if not coaching:
        raise CoachingNotFoundError

    link = await db.get(models.CalendlyLink, user_id=instructor)
    if not link:
        raise CoachingNotFoundError

    if not await shop.spend_coins(user.id, coaching.price):
        raise NotEnoughCoinsError
    await shop.add_coins(instructor, int(coaching.price * (1 - settings.event_fee)))

    return await calendly.create_single_use_link(link.api_token, link.uri)


@router.put(
    "/coachings/{skill_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(Coaching, SkillRequirementsNotMetError, CalendlyNotConfiguredError),
)
async def set_coaching(data: UpdateCoaching, skill_id: str, user: User = user_auth) -> Any:
    """
    Set up a coaching for a skill.

    *Requirements:* **VERIFIED**
    """

    if not user.admin and not {settings.coaching_skill, skill_id}.issubset(await get_completed_skills(user.id)):
        raise SkillRequirementsNotMetError

    if not await calendly.get_calendly_link(user.id):
        raise CalendlyNotConfiguredError

    coaching = await db.get(models.Coaching, user_id=user.id, skill_id=skill_id)
    if not coaching:
        await db.add(coaching := models.Coaching(user_id=user.id, skill_id=skill_id, price=data.price))
    else:
        coaching.price = data.price

    return Coaching(instructor=coaching.user_id, skill_id=coaching.skill_id, price=coaching.price)


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

    return True
