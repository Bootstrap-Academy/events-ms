"""Endpoints related to the calendar."""

import hmac
from datetime import datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter
from fastapi.responses import Response
from sqlalchemy import or_

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db, select
from api.exceptions.auth import PermissionDeniedError, verified_responses
from api.exceptions.slots import SlotNotFoundException
from api.schemas.calendar import Calendar, Event, EventType
from api.schemas.user import User
from api.services import shop
from api.services.auth import get_instructor, is_admin
from api.services.ics import create_ics
from api.services.skills import get_skills
from api.settings import settings
from api.utils.cache import clear_cache, redis_cached


router = APIRouter()


@redis_cached("calendar", "user_id", "booked_only", "admin")
async def get_events(user_id: str, booked_only: bool, admin: bool) -> list[Event]:
    out = []
    webinar: models.Webinar
    async for webinar in await db.stream(select(models.Webinar)):
        booked = user_id == webinar.creator or any(
            participant.user_id == user_id for participant in webinar.participants
        )
        if booked_only and not booked:
            continue

        out.append(
            Event(
                id=webinar.id,
                title=webinar.name,
                description=webinar.description,
                start=webinar.start.timestamp(),
                end=webinar.end.timestamp(),
                location=webinar.link if booked or admin else None,
                type=EventType.WEBINAR,
                instructor=await get_instructor(webinar.creator),
                student=None,
                skill_id=webinar.skill_id,
                booked=booked,
            )
        )

    slot: models.Slot
    async for slot in await db.stream(
        select(models.Slot).where(or_(models.Slot.user_id == user_id, models.Slot.booked_by == user_id))
    ):
        if booked_only and not slot.booked:
            continue

        out.append(
            Event(
                id=slot.id,
                title="Open Slot" if not slot.booked else None,
                description=None,
                start=slot.start.timestamp(),
                end=slot.end.timestamp(),
                location=slot.meeting_link,
                type=EventType[slot.event_type.name] if slot.event_type else None,
                instructor=await get_instructor(slot.user_id) if user_id != slot.booked_by or admin else None,
                student=await get_instructor(slot.booked_by) if slot.booked_by else None,
                skill_id=slot.skill_id,
                booked=slot.booked,
            )
        )

    return out


@router.get("/calendar", dependencies=[require_verified_email], responses=verified_responses(Calendar))
@redis_cached("calendar", "user")
async def get_calendar(user: User = user_auth) -> Any:
    """
    Return the private calendar url for the user.

    *Requirements:* **VERIFIED**
    """

    token = hmac.digest(settings.calendar_secret.encode(), user.id.encode(), "sha256").hex()
    return Calendar(
        ics=f"{settings.public_base_url.rstrip('/')}/calendar/{user.id}/{token}0/academy.ics",
        ics_booked_only=f"{settings.public_base_url.rstrip('/')}/calendar/{user.id}/{token}1/academy.ics",
        events=await get_events(user.id, False, user.admin),
    )


@router.get("/calendar/{user_id}/{token}/academy.ics", include_in_schema=False)
@redis_cached("calendar", "user_id", "token")
async def download_ics(user_id: str, token: str) -> Any:
    if hmac.digest(settings.calendar_secret.encode(), user_id.encode(), "sha256").hex() != token[:-1]:
        return Response(status_code=401)

    admin = await is_admin(user_id)
    booked_only = token[-1] == "1"

    skills = {s.id: s.name for s in await get_skills()}
    events = await get_events(user_id, booked_only, admin)
    for e in events:
        if e.type == EventType.WEBINAR:
            e.title = f"Webinar: {e.title}"
            e.description = e.description or ""
            if not e.booked and settings.webinar_registration_url:
                url = settings.webinar_registration_url.replace("WEBINAR_ID", e.id)
                e.description = (
                    f"You are not registered for this webinar. Register now: {url}\n\n{e.description.strip()}"
                )
            if e.instructor:
                e.description = f"{e.description.strip()}\n\nInstructor: {e.instructor}"
        elif e.type == EventType.COACHING:
            e.title = "Coaching"
            e.description = e.description or ""
            if skill := skills.get(cast(str, e.skill_id)):
                e.title += f": {skill}"
            if e.instructor and user_id != e.instructor.id:
                e.description = f"Instructor: {e.instructor}"
            elif e.student and user_id != e.student.id:
                e.description = f"Student: {e.student}"
            if settings.event_cancel_url:
                url = settings.event_cancel_url.replace("EVENT_ID", e.id)
                e.description = f"{e.description.strip()}\n\nCancel event: {url}"
            e.description = e.description.strip()
        elif e.type == EventType.EXAM:
            e.title = "Exam"
            e.description = e.description or ""
            if skill := skills.get(cast(str, e.skill_id)):
                e.title += f": {skill}"
            if e.student and user_id != e.student.id:
                e.description = f"Student: {e.student}"
            if settings.event_cancel_url:
                url = settings.event_cancel_url.replace("EVENT_ID", e.id)
                e.description = f"{e.description.strip()}\n\nCancel event: {url}"
            e.description = e.description.strip()

    return Response(create_ics(events), media_type="text/calendar")


@router.delete(
    "/calendar/{event_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(bool, SlotNotFoundException),
)
async def cancel_event(event_id: str, user: User = user_auth) -> Any:
    """
    Cancel a coaching or exam.

    The user must either be the instructor or the participant, or an admin.
    Webinars cannot be cancelled via this endpoint.

    *Requirements:* **VERIFIED**
    """

    slot = await db.get(models.Slot, id=event_id)
    if not slot or not slot.booked_by or slot.instructor_coins is None or slot.student_coins is None:
        raise SlotNotFoundException

    if user.id != slot.user_id and user.id != slot.booked_by and not user.admin:
        raise SlotNotFoundException

    delta = slot.start - datetime.now()
    if user.id == slot.booked_by and not user.admin and delta < timedelta(0):
        raise PermissionDeniedError
    if user.id == slot.user_id and not user.admin and delta < timedelta(days=1):
        raise PermissionDeniedError

    if user.id != slot.booked_by or user.admin:
        student_coins = slot.student_coins
        instructor_coins = 0
    elif delta >= timedelta(days=3):
        student_coins = slot.student_coins
        instructor_coins = 0
    elif delta >= timedelta(days=1):
        student_coins = slot.student_coins // 2
        instructor_coins = slot.instructor_coins // 2
    else:
        student_coins = 0
        instructor_coins = slot.instructor_coins

    if student_coins:
        await shop.add_coins(slot.booked_by, student_coins)
    if instructor_coins:
        await shop.add_coins(slot.user_id, instructor_coins)

    slot.cancel()
    # todo: email

    await clear_cache("calendar")

    return True
