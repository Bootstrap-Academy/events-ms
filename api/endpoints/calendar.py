"""Endpoints related to the calendar."""

import hmac
from datetime import timedelta
from typing import Any, Type, cast

from fastapi import APIRouter, Path, Query
from sqlalchemy import func
from sqlalchemy.sql import Select
from starlette.responses import Response

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db, select
from api.exceptions.auth import PermissionDeniedError, verified_responses
from api.exceptions.slots import SlotNotFoundException
from api.schemas.calendar import Calendar, Coaching, EventType, Webinar
from api.schemas.user import User
from api.services import shop
from api.services.auth import get_userinfo, is_admin
from api.services.ics import create_ics
from api.services.skills import get_skill_levels
from api.settings import settings
from api.utils.cache import clear_cache
from api.utils.utc import utcfromtimestamp, utcnow


router = APIRouter()


def _filter_time(
    query: Select,
    cls: Type[models.Webinar | models.Slot],
    start_after: int | None,
    start_before: int | None,
    duration_min: int | None,
    duration_max: int | None,
) -> Select:
    if start_after:
        query = query.where(cls.start >= utcfromtimestamp(start_after))
    if start_before:
        query = query.where(cls.start <= utcfromtimestamp(start_before))
    if duration_min:
        query = query.where(cls.end - cls.start >= timedelta(minutes=duration_min))
    if duration_max:
        query = query.where(cls.end - cls.start <= timedelta(minutes=duration_max))
    return query


async def get_webinars(
    user_id: str,
    admin: bool,
    title: str | None,
    description: str | None,
    instructor_id: str | None,
    skill_id: str | None,
    start_after: int | None,
    start_before: int | None,
    duration_min: int | None,
    duration_max: int | None,
) -> list[Webinar]:
    events = []
    query = select(models.Webinar).where(models.Webinar.end > utcnow())
    if title:
        query = query.where(func.lower(models.Webinar.name).contains(title.lower(), autoescape=True))
    if description:
        query = query.where(func.lower(models.Webinar.description).contains(description.lower(), autoescape=True))
    if instructor_id:
        query = query.filter_by(creator=instructor_id)
    if skill_id:
        query = query.filter_by(skill_id=skill_id)
    query = _filter_time(query, models.Webinar, start_after, start_before, duration_min, duration_max)

    webinar: models.Webinar
    async for webinar in await db.stream(query):
        _booked = user_id == webinar.creator or any(
            participant.user_id == user_id for participant in webinar.participants
        )
        _bookable = not _booked and utcnow() < webinar.start and len(webinar.participants) < webinar.max_participants

        events.append(
            Webinar(
                id=webinar.id,
                type=EventType.WEBINAR,
                title=webinar.name,
                description=webinar.description,
                skill_id=webinar.skill_id,
                start=int(webinar.start.timestamp()),
                duration=int((webinar.end - webinar.start).total_seconds()) // 60,
                price=webinar.price,
                admin_link=webinar.admin_link if admin or user_id == webinar.creator else None,
                link=(
                    webinar.link
                    if admin or user_id == webinar.creator or (_booked and webinar.start - utcnow() < timedelta(days=1))
                    else None
                ),
                instructor=await get_userinfo(webinar.creator),
                instructor_rating=await models.LecturerRating.get_rating(webinar.creator, webinar.skill_id),
                booked=_booked,
                bookable=_bookable,
                creation_date=int(webinar.creation_date.timestamp()),
                max_participants=webinar.max_participants,
                participants=len(webinar.participants),
            )
        )
    return events


async def get_coachings(
    user_id: str,
    admin: bool,
    instructor_id: str | None,
    start_after: int | None,
    start_before: int | None,
    duration_min: int | None,
    duration_max: int | None,
) -> list[Coaching]:
    events = []
    query = select(models.Slot).where(models.Slot.end > utcnow())
    query = _filter_time(query, models.Slot, start_after, start_before, duration_min, duration_max)
    if instructor_id:
        query = query.filter_by(user_id=instructor_id)

    coachings: dict[str, dict[str, int]] = {}
    coaching: models.Coaching
    async for coaching in await db.stream(select(models.Coaching)):
        coachings.setdefault(coaching.user_id, {})[coaching.skill_id] = coaching.price

    slot: models.Slot
    async for slot in await db.stream(query):
        if slot.booked:
            events.append(
                Coaching(
                    id=slot.id,
                    type=EventType.COACHING,
                    title=None,
                    description=None,
                    skill_id=slot.skill_id,
                    start=int(slot.start.timestamp()),
                    duration=int((slot.end - slot.start).total_seconds()) // 60,
                    price=cast(int, slot.student_coins),
                    admin_link=slot.admin_link if admin or user_id == slot.user_id else None,
                    link=slot.link if admin or user_id in (slot.user_id, slot.booked_by) else None,
                    instructor=await get_userinfo(slot.user_id),
                    instructor_rating=await models.LecturerRating.get_rating(slot.user_id, slot.skill_id),
                    booked=True,
                    bookable=False,
                    student=(
                        await get_userinfo(slot.booked_by)
                        if admin or user_id in (slot.user_id, slot.booked_by)
                        else None
                    ),
                )
            )
            continue

        for skill, price in coachings.get(slot.user_id, {}).items():
            level = (await get_skill_levels(slot.user_id)).get(skill, 0)
            if level < settings.coaching_level and not await is_admin(slot.user_id):
                continue

            events.append(
                Coaching(
                    id=slot.id,
                    type=EventType.COACHING,
                    title=None,
                    description=None,
                    skill_id=skill,
                    start=int(slot.start.timestamp()),
                    duration=int((slot.end - slot.start).total_seconds()) // 60,
                    price=price,
                    admin_link=None,
                    link=None,
                    instructor=await get_userinfo(slot.user_id),
                    instructor_rating=await models.LecturerRating.get_rating(slot.user_id, skill),
                    booked=False,
                    bookable=user_id != slot.user_id,
                    student=None,
                )
            )
    return events


async def get_events(
    user_id: str,
    admin: bool,
    type_: EventType | None,
    title: str | None,
    description: str | None,
    instructor_id: str | None,
    skill_id: str | None,
    start_after: int | None,
    start_before: int | None,
    duration_min: int | None,
    duration_max: int | None,
    price_min: int | None,
    price_max: int | None,
    booked: bool | None,
    bookable: bool | None,
) -> list[Webinar | Coaching]:
    events: list[Webinar | Coaching] = []
    if type_ is None or type_ == EventType.WEBINAR:
        events += await get_webinars(
            user_id,
            admin,
            title,
            description,
            instructor_id,
            skill_id,
            start_after,
            start_before,
            duration_min,
            duration_max,
        )
    if type_ is None or type_ == EventType.COACHING:
        events += await get_coachings(
            user_id, admin, instructor_id, start_after, start_before, duration_min, duration_max
        )

    free = {ec.user_id async for ec in await db.stream(select(models.EmergencyCancel))}
    for event in events:
        if event.instructor and event.instructor.id in free:
            event.price = 0

    f = iter(events)
    f = filter(lambda e: skill_id is None or skill_id == e.skill_id, f)
    f = filter(lambda e: price_min is None or e.price >= price_min, f)
    f = filter(lambda e: price_max is None or e.price <= price_max, f)
    f = filter(lambda e: booked is None or e.booked is booked, f)
    f = filter(lambda e: bookable is None or e.bookable is bookable, f)

    return [*f]


@router.get("/calendar", dependencies=[require_verified_email], responses=verified_responses(Calendar))
async def get_calendar(
    type_: EventType | None = Query(None, alias="type", description="Return only events of this type"),
    title: str | None = Query(None, description="Return only events with this title"),
    description: str | None = Query(None, description="Return only events with this description"),
    instructor_id: str | None = Query(None, description="Return only events created by this user"),
    skill_id: str | None = Query(None, description="Return only events with this skill id"),
    start_after: int | None = Query(None, description="Return only events that start after this timestamp"),
    start_before: int | None = Query(None, description="Return only events that start before this timestamp"),
    duration_min: int | None = Query(None, description="Return only events that last at least this long (in minutes)"),
    duration_max: int | None = Query(None, description="Return only events that last at most this long (in minutes)"),
    price_min: int | None = Query(None, description="Return only events that cost at least this much (in morphcoins)"),
    price_max: int | None = Query(None, description="Return only events that cost at most this much (in morphcoins)"),
    booked: bool | None = Query(None, description="Return only events that the user has booked"),
    bookable: bool | None = Query(None, description="Return only events that the user can book"),
    user: User = user_auth,
) -> Any:
    """
    Return the calendar for the user.

    *Requirements:* **VERIFIED**
    """

    events = await get_events(
        user.id,
        user.admin,
        type_,
        title,
        description,
        instructor_id,
        skill_id,
        start_after,
        start_before,
        duration_min,
        duration_max,
        price_min,
        price_max,
        booked,
        bookable,
    )

    return Calendar(
        ics_token=f"{user.id}_" + hmac.digest(settings.calendar_secret.encode(), user.id.encode(), "sha256").hex(),
        events=events,
    )


@router.get("/calendar/{token}/academy.ics")
async def download_ics(
    type_: EventType | None = Query(None, alias="type", description="Return only events of this type"),
    skill_id: str | None = Query(None, description="Return only events with this skill id"),
    booked: bool | None = Query(None, description="Return only events that the user has booked"),
    bookable: bool | None = Query(None, description="Return only events that the user can book"),
    token: str = Path(regex=r"^[^_]+_[^_]+$"),
) -> Any:
    """wip"""

    user_id, token = token.split("_")
    if token != hmac.digest(settings.calendar_secret.encode(), user_id.encode(), "sha256").hex():
        return Response(status_code=401)

    admin = await is_admin(user_id)

    events = await get_events(
        user_id, admin, type_, None, None, None, skill_id, None, None, None, None, None, None, booked, bookable
    )

    return Response(await create_ics(events), media_type="text/calendar")


@router.delete(
    "/calendar/{event_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(bool, SlotNotFoundException, PermissionDeniedError),
)
async def cancel_event(event_id: str, user: User = user_auth) -> Any:
    """
    Cancel a webinar or coaching.

    The user must either be the instructor or the participant, or an admin.

    *Requirements:* **VERIFIED**
    """

    if await _try_cancel_webinar(event_id, user):
        return True
    if await _try_cancel_coaching(event_id, user):
        return True
    raise SlotNotFoundException


async def _try_cancel_webinar(event_id: str, user: User = user_auth) -> bool:
    webinar = await db.get(models.Webinar, id=event_id)
    if webinar is None:
        return False

    participant = next((p for p in webinar.participants if p.user_id == user.id), None)
    if not user.admin and not participant and webinar.creator != user.id:
        return False

    delta = webinar.start - utcnow()

    if delta < timedelta(0):
        # cannot cancel events that have already started
        raise PermissionDeniedError

    if participant:
        if delta >= timedelta(days=7):
            student_coins = webinar.price
            instructor_coins = 0
        elif delta >= timedelta(days=1):
            student_coins = webinar.price // 2
            instructor_coins = int(webinar.price * (1 - settings.event_fee) // 2)
        else:
            raise PermissionDeniedError

        if student_coins:
            await shop.add_coins(participant.user_id, student_coins, f"Cancel webinar '{webinar.name}'", False)
        if instructor_coins:
            await shop.add_coins(webinar.creator, instructor_coins, f"Cancel webinar '{webinar.name}'", False)

        await db.delete(participant)
        await clear_cache("calendar")
        # todo: email?

        return True

    for participant in webinar.participants:
        await shop.spend_coins(participant.user_id, webinar.price, f"Webinar {webinar.name}")

    if webinar.participants:
        await models.EmergencyCancel.create(webinar.creator)

    await db.delete(webinar)
    await clear_cache("calendar")
    # todo: email

    return True


async def _try_cancel_coaching(event_id: str, user: User = user_auth) -> bool:
    slot = await db.get(models.Slot, id=event_id)
    if not slot or not slot.booked_by or slot.instructor_coins is None or slot.student_coins is None:
        return False

    if user.id != slot.user_id and user.id != slot.booked_by and not user.admin:
        return False

    delta = slot.start - utcnow()
    student_coins = instructor_coins = 0

    if delta < timedelta(0):
        # cannot cancel events that have already started
        raise PermissionDeniedError

    if user.id == slot.booked_by:  # student cancels
        if delta >= timedelta(days=7):
            student_coins = slot.student_coins
        elif delta >= timedelta(days=1):
            student_coins = slot.student_coins // 2
            instructor_coins = slot.instructor_coins // 2
        else:
            raise PermissionDeniedError
    elif user.id == slot.user_id and slot.booked:  # instructor cancels
        student_coins = slot.student_coins
        await models.EmergencyCancel.create(slot.user_id)

    if student_coins:
        await shop.add_coins(slot.booked_by, student_coins, "Cancel coaching", False)
    if instructor_coins:
        await shop.add_coins(slot.user_id, instructor_coins, "Cancel coaching", False)

    slot.cancel()
    # todo: email

    await clear_cache("calendar")

    return True
