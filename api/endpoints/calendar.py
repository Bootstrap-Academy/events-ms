"""Endpoints related to the calendar."""

from datetime import timedelta
from typing import Any, Callable, Coroutine, Type, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.routing import APIRoute
from sqlalchemy import func
from sqlalchemy.sql import Select
from starlette.responses import Response

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db, select
from api.exceptions.auth import verified_responses
from api.schemas.calendar import Calendar, CalendarToken, Coaching, EventType, Webinar
from api.schemas.ordinary_cancellation import CancellationDeclaration, CancellationPreparation
from api.schemas.user import User
from api.services import booking_contracts, booking_payments, ordinary_cancellations
from api.services.auth import get_userinfo, is_admin
from api.services.ics import create_ics
from api.services.skills import get_skill_levels
from api.settings import settings
from api.utils.utc import utcfromtimestamp, utcnow


class CancellationReceiptRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        if self.path != "/calendar/cancellations/{command_id}" or "POST" not in self.methods:
            return handler

        async def record_body_receipt(request: Request) -> Response:
            # Complete body arrival precedes awaited token/current-authority checks.
            # FastAPI reuses these cached bytes and still validates the schema.
            await request.body()
            request.state.ordinary_declaration_received_at = utcnow()
            return await handler(request)

        return record_body_receipt


router = APIRouter(route_class=CancellationReceiptRoute)


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
        participation = next((p for p in webinar.participants if p.user_id == user_id), None)
        payment = await booking_payments.payment_for(participation) if participation else None
        _booked = user_id == webinar.creator or any(
            participant.user_id == user_id for participant in webinar.participants
        )
        _bookable = (
            not _booked
            and not webinar.closed_to_new_bookings
            and utcnow() < webinar.start
            and len(webinar.participants) < webinar.max_participants
        )

        events.append(
            Webinar(
                id=webinar.id,
                type=EventType.WEBINAR,
                title=webinar.name,
                description=webinar.description,
                skill_id=webinar.skill_id,
                start=int(webinar.start.timestamp()),
                duration=int((webinar.end - webinar.start).total_seconds()) // 60,
                price=payment.paid_coins if payment else webinar.price,
                payment_state=await booking_contracts.customer_state(payment),
                admin_link=webinar.admin_link if admin or user_id == webinar.creator else None,
                link=(
                    webinar.link
                    if admin
                    or user_id == webinar.creator
                    or (
                        _booked
                        and webinar.start - utcnow() < timedelta(days=1)
                        and await booking_contracts.ready(payment)
                    )
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
            payment = await booking_payments.payment_for(slot) if slot.payment_id else None
            events.append(
                Coaching(
                    id=slot.id,
                    type=EventType.COACHING,
                    title=None,
                    description=None,
                    skill_id=slot.skill_id,
                    start=int(slot.start.timestamp()),
                    duration=int((slot.end - slot.start).total_seconds()) // 60,
                    price=slot.student_coins,
                    payment_state=await booking_contracts.customer_state(payment),
                    admin_link=slot.admin_link if admin or user_id == slot.user_id else None,
                    link=(
                        slot.link
                        if admin
                        or user_id == slot.user_id
                        or (user_id == slot.booked_by and await booking_contracts.ready(payment))
                        else None
                    ),
                    instructor=await get_userinfo(slot.user_id),
                    instructor_rating=await models.LecturerRating.get_rating(slot.user_id, slot.skill_id),
                    booked=True,
                    bookable=False,
                    student=(
                        await get_userinfo(cast(str, slot.booked_by))
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
        if event.bookable and event.instructor and event.instructor.id in free:
            event.price = 0

    f = iter(events)
    f = filter(lambda e: skill_id is None or skill_id == e.skill_id, f)
    f = filter(lambda e: price_min is None or e.price is None or e.price >= price_min, f)
    f = filter(lambda e: price_max is None or e.price is None or e.price <= price_max, f)
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

    return Calendar(ics_token=(await models.CalendarToken.get_or_create(user.id)).token, events=events)


@router.post(
    "/calendar/token/rotate", dependencies=[require_verified_email], responses=verified_responses(CalendarToken)
)
async def rotate_ics_token(user: User = user_auth) -> Any:
    """
    Create a new token for the ics calendar feed of the user and invalidate the previous one.

    Calendar clients that use the old subscription url stop receiving updates immediately.

    *Requirements:* **VERIFIED**
    """

    return CalendarToken(ics_token=(await models.CalendarToken.rotate(user.id)).token)


@router.get("/calendar/{token}/academy.ics")
async def download_ics(
    type_: EventType | None = Query(None, alias="type", description="Return only events of this type"),
    skill_id: str | None = Query(None, description="Return only events with this skill id"),
    booked: bool | None = Query(None, description="Return only events that the user has booked"),
    bookable: bool | None = Query(None, description="Return only events that the user can book"),
    token: str = Path(regex=r"^[A-Za-z0-9_-]{16,128}$"),
) -> Any:
    """Return the calendar of the user the token belongs to as an ics file."""

    user_id = await models.CalendarToken.get_user_id(token)
    if user_id is None:
        return Response(status_code=401)

    admin = await is_admin(user_id)

    events = await get_events(
        user_id, admin, type_, None, None, None, skill_id, None, None, None, None, None, None, booked, bookable
    )

    return Response(await create_ics(events), media_type="text/calendar")


@router.post("/calendar/{event_id}/cancellation-target", dependencies=[require_verified_email])
async def prepare_cancellation(event_id: str, body: CancellationPreparation, user: User = user_auth) -> dict[str, Any]:
    return await ordinary_cancellations.prepare(user, event_id, body)


@router.post("/calendar/cancellations/{command_id}", dependencies=[require_verified_email])
async def declare_cancellation(
    command_id: UUID, body: CancellationDeclaration, request: Request, user: User = user_auth
) -> dict[str, Any]:
    return await ordinary_cancellations.receive(
        user, str(command_id), body, received_at=request.state.ordinary_declaration_received_at
    )


@router.get("/calendar/cancellations/{command_id}", dependencies=[require_verified_email])
async def cancellation_receipt(command_id: UUID, user: User = user_auth) -> dict[str, Any]:
    return await ordinary_cancellations.status(user, str(command_id))


@router.delete("/calendar/{event_id}", dependencies=[require_verified_email])
async def cancel_event(event_id: str, user: User = user_auth) -> Any:
    # Old event-only retries cannot distinguish a replaced original order.
    raise HTTPException(409, detail={"code": "ExactCancellationTargetRequired", "cancellation_recorded": False})
