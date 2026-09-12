"""Endpoints related to webinars."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db, filter_by
from api.endpoints.closed_offerings import closed_offering
from api.exceptions.auth import PermissionDeniedError, verified_responses
from api.exceptions.coaching import NotEnoughCoinsError
from api.exceptions.skills import SkillRequirementsNotMetError
from api.exceptions.webinars import (
    AlreadyFullError,
    AlreadyRegisteredError,
    CannotStartInPastError,
    InsufficientRatingError,
    WebinarNotFoundError,
)
from api.schemas.calendar import Webinar
from api.schemas.user import User
from api.schemas.webinars import CreateWebinar, UpdateWebinar
from api.services import booking_contracts, booking_payments, retained_events
from api.services.skills import get_skill_levels
from api.settings import settings
from api.utils.cache import clear_cache
from api.utils.utc import utcfromtimestamp, utcnow


router = APIRouter()


@Depends
async def get_webinar(webinar_id: str) -> models.Webinar:
    webinar = await db.get(models.Webinar, id=webinar_id)
    if not webinar:
        raise WebinarNotFoundError

    return webinar


@Depends
async def can_manage_webinar(webinar: models.Webinar = get_webinar, user: User = user_auth) -> None:
    if webinar.creator != user.id and not user.admin:
        raise PermissionDeniedError


async def check_price(user_id: str, skill_id: str, price: int, max_participants: int) -> None:
    rating = await models.LecturerRating.get_rating(user_id, skill_id) or 0
    mx: int | None = None
    if rating < 3:
        mx = 0
    elif rating < 4:
        mx = 5000
    elif rating < 4.5:
        mx = 10000
    if mx is not None and price * max_participants > mx:
        raise InsufficientRatingError(mx // max_participants)


@router.post(
    "/webinars",
    dependencies=[closed_offering, require_verified_email],
    responses=verified_responses(
        Webinar, SkillRequirementsNotMetError, CannotStartInPastError, InsufficientRatingError
    ),
    deprecated=True,
)
async def create_webinar(data: CreateWebinar, user: User = user_auth) -> Any:
    """
    Create a new webinar.

    *Requirements:* **VERIFIED**
    """

    await retained_events.require_current_subject(user.id)
    if not user.admin and (await get_skill_levels(user.id)).get(data.skill_id, 0) < settings.webinar_level:
        raise SkillRequirementsNotMetError

    now = utcnow()
    if data.start <= now.timestamp():
        raise CannotStartInPastError

    if not user.admin:
        await check_price(user.id, data.skill_id, data.price, data.max_participants)

    webinar = models.Webinar(
        id=str(uuid4()),
        skill_id=data.skill_id,
        creator=user.id,
        creation_date=now,
        xp_delivery_protocol=1,  # new session created by the keyed-benefit producer
        name=data.name,
        description=data.description,
        admin_link=data.admin_link or data.link,
        link=data.link,
        start=utcfromtimestamp(data.start),
        end=utcfromtimestamp(data.start + data.duration * 60),
        max_participants=data.max_participants,
        price=data.price,
        participants=[],
    )
    await db.add(webinar)

    await clear_cache("calendar")

    return await webinar.serialize(True, True, True, False)


@router.get(
    "/webinars/{webinar_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(Webinar, WebinarNotFoundError),
)
async def get_webinar_by_id(webinar: models.Webinar = get_webinar, user: User = user_auth) -> Any:
    """
    Get a webinar by id.

    The `link` is included iff the user has registered for the webinar, has created this webinar or is an admin.

    *Requirements:* **VERIFIED**
    """

    _booked = user.id == webinar.creator or any(participant.user_id == user.id for participant in webinar.participants)
    # Existing bookings remain readable; this is no longer a public offer.
    _bookable = False
    include_link = (
        user.admin or user.id == webinar.creator or (_booked and webinar.start - utcnow() < timedelta(days=1))
    )

    if include_link and not (user.admin or user.id == webinar.creator):
        participant = next((p for p in webinar.participants if p.user_id == user.id), None)
        include_link = bool(participant) and await booking_contracts.ready(
            await booking_payments.payment_for(participant)
        )
    return await webinar.serialize(include_link, user.admin or user.id == webinar.creator, _booked, _bookable)


@router.get(
    "/webinars/{webinar_id}/participants",
    dependencies=[require_verified_email, can_manage_webinar],
    responses=verified_responses(list[str], WebinarNotFoundError, PermissionDeniedError),
)
async def list_webinar_participants(webinar: models.Webinar = get_webinar) -> Any:
    """
    Return a list of all participants of a webinar.

    Can only be accessed by the webinar host or an admin.

    *Requirements:* **VERIFIED**
    """

    return [participant.user_id for participant in webinar.participants]


@router.post(
    "/webinars/{webinar_id}/participants",
    dependencies=[closed_offering, require_verified_email],
    responses=verified_responses(
        Webinar, WebinarNotFoundError, AlreadyRegisteredError, AlreadyFullError, NotEnoughCoinsError
    ),
    deprecated=True,
)
async def register_for_webinar(
    data: booking_contracts.Acceptance, webinar: models.Webinar = get_webinar, user: User = user_auth
) -> Any:
    """
    Register for a webinar.

    *Requirements:* **VERIFIED**
    """

    await retained_events.require_current_subject(user.id)
    locked_webinar = await db.first(
        filter_by(models.Webinar, id=webinar.id).with_for_update().execution_options(populate_existing=True)
    )
    if locked_webinar is None or locked_webinar.start < utcnow():
        raise WebinarNotFoundError
    webinar = locked_webinar

    participants = await db.all(
        filter_by(models.WebinarParticipant, webinar_id=webinar.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    existing = next((p for p in participants if p.user_id == user.id), None)
    if existing is not None:
        payment = await booking_payments.payment_for(existing)
        if str(data.order_id) == payment.id:
            await booking_contracts.prepare(user.id, "webinar", webinar, data)
            await booking_payments.finish(payment.id)
            return await webinar.serialize(
                webinar.start - utcnow() < timedelta(days=1) and await booking_contracts.ready(payment),
                False,
                True,
                False,
            )
        raise AlreadyRegisteredError
    if user.id == webinar.creator:
        raise AlreadyRegisteredError

    if webinar.closed_to_new_bookings or len(participants) >= webinar.max_participants:
        raise AlreadyFullError

    contract = await booking_contracts.prepare(user.id, "webinar", webinar, data)
    emergency = bool(contract.offer["product"]["facts"]["emergency_waiver"])
    if emergency and not await models.EmergencyCancel.delete(webinar.creator):
        raise HTTPException(409, "Emergency waiver unavailable; no booking was placed")
    paid_coins = contract.offer["product"]["coins"]
    payment = await booking_payments.reserve(
        "webinar", webinar.id, user.id, paid_coins, f"Webinar '{webinar.name}'", emergency, payment_id=contract.id
    )
    webinar.participants.append(
        models.WebinarParticipant(
            user_id=user.id, webinar_id=webinar.id, paid_coins=payment.paid_coins, payment_id=payment.id
        )
    )
    await retained_events.record_booking_reservation(payment)
    await booking_payments.finish(payment.id)

    await clear_cache("calendar")

    include_link = webinar.start - utcnow() < timedelta(days=1)
    include_link = include_link and await booking_contracts.ready(payment)

    return await webinar.serialize(include_link, False, True, False)


@router.patch(
    "/webinars/{webinar_id}",
    dependencies=[closed_offering, require_verified_email, can_manage_webinar],
    responses=verified_responses(Webinar, WebinarNotFoundError, PermissionDeniedError, CannotStartInPastError),
    deprecated=True,
)
async def update_webinar(data: UpdateWebinar, user: User = user_auth, webinar: models.Webinar = get_webinar) -> Any:
    """
    Update a webinar.

    Can only be accessed by the webinar host or an admin.

    *Requirements:* **VERIFIED**
    """

    locked = await db.first(
        filter_by(models.Webinar, id=webinar.id).with_for_update().execution_options(populate_existing=True)
    )
    if locked is None:
        raise HTTPException(404, "Webinar unavailable")
    webinar = locked
    if webinar.participants and any(
        (
            data.name is not None and data.name != webinar.name,
            data.description is not None and data.description != webinar.description,
            data.start is not None and data.start != webinar.start.timestamp(),
            data.duration is not None and data.duration != int((webinar.end - webinar.start).total_seconds()) // 60,
        )
    ):
        raise HTTPException(
            409,
            "Booked terms cannot be replaced. Cancel the original event or offer an explicitly accepted alternative.",
        )
    if data.name is not None and data.name != webinar.name:
        webinar.name = data.name

    if data.description is not None and data.description != webinar.description:
        webinar.description = data.description

    if data.admin_link is not None and data.admin_link != webinar.admin_link:
        webinar.admin_link = data.admin_link

    if data.link is not None and data.link != webinar.link:
        webinar.link = data.link

    if data.start is not None and data.start != webinar.start.timestamp():
        if data.start <= utcnow().timestamp():
            raise CannotStartInPastError
        webinar.end += utcfromtimestamp(data.start) - webinar.start
        webinar.start = utcfromtimestamp(data.start)

    if data.duration is not None:
        webinar.end = utcfromtimestamp(webinar.start.timestamp() + data.duration * 60)

    if data.max_participants is not None and data.max_participants != webinar.max_participants:
        webinar.max_participants = data.max_participants

    if data.price is not None and data.price != webinar.price:
        webinar.price = data.price

    if not user.admin:
        await check_price(user.id, webinar.skill_id, webinar.price, webinar.max_participants)

    await clear_cache("calendar")

    return await webinar.serialize(True, True, True, False)


@router.post("/webinars/{webinar_id}/offer", dependencies=[closed_offering, require_verified_email], deprecated=True)
async def webinar_offer(webinar: models.Webinar = get_webinar, user: User = user_auth) -> Any:
    await retained_events.require_current_subject(user.id)
    locked = await db.first(
        filter_by(models.Webinar, id=webinar.id).with_for_update().execution_options(populate_existing=True)
    )
    if locked is None or locked.closed_to_new_bookings or locked.start <= utcnow() or user.id == locked.creator:
        raise WebinarNotFoundError
    return await booking_contracts.offer(user.id, "webinar", locked)
