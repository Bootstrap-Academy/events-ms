"""Endpoints related to webinars."""

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db
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
from api.services import shop
from api.services.auth import get_email
from api.services.skills import get_skill_levels
from api.settings import settings
from api.utils.cache import clear_cache
from api.utils.email import BOOKED_WEBINAR
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
    dependencies=[require_verified_email],
    responses=verified_responses(
        Webinar, SkillRequirementsNotMetError, CannotStartInPastError, InsufficientRatingError
    ),
)
async def create_webinar(data: CreateWebinar, user: User = user_auth) -> Any:
    """
    Create a new webinar.

    *Requirements:* **VERIFIED**
    """

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
    _bookable = not _booked and utcnow() < webinar.start and len(webinar.participants) < webinar.max_participants

    return await webinar.serialize(user.admin or _booked, user.admin or user.id == webinar.creator, _booked, _bookable)


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
    dependencies=[require_verified_email],
    responses=verified_responses(
        Webinar, WebinarNotFoundError, AlreadyRegisteredError, AlreadyFullError, NotEnoughCoinsError
    ),
)
async def register_for_webinar(webinar: models.Webinar = get_webinar, user: User = user_auth) -> Any:
    """
    Register for a webinar.

    *Requirements:* **VERIFIED**
    """

    if webinar.start < utcnow():
        raise WebinarNotFoundError

    if user.id == webinar.creator or any(participant.user_id == user.id for participant in webinar.participants):
        raise AlreadyRegisteredError

    if len(webinar.participants) >= webinar.max_participants:
        raise AlreadyFullError

    if not await models.EmergencyCancel.exists(webinar.creator) and not await shop.spend_coins(user.id, webinar.price):
        raise NotEnoughCoinsError

    webinar.participants.append(models.WebinarParticipant(user_id=user.id, webinar_id=webinar.id))

    await clear_cache("calendar")

    if email := await get_email(user.id):
        await BOOKED_WEBINAR.send(
            email,
            title=webinar.name,
            date=webinar.start.strftime("%d.%m.%Y"),
            time=webinar.start.strftime("%H:%M"),
            link=webinar.link,
            coins=webinar.price,
        )

    return await webinar.serialize(True, False, True, False)


@router.patch(
    "/webinars/{webinar_id}",
    dependencies=[require_verified_email, can_manage_webinar],
    responses=verified_responses(Webinar, WebinarNotFoundError, PermissionDeniedError, CannotStartInPastError),
)
async def update_webinar(data: UpdateWebinar, user: User = user_auth, webinar: models.Webinar = get_webinar) -> Any:
    """
    Update a webinar.

    Can only be accessed by the webinar host or an admin.

    *Requirements:* **VERIFIED**
    """

    if data.name is not None and data.name != webinar.name:
        webinar.name = data.name

    if data.description is not None and data.description != webinar.description:
        webinar.description = data.description

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
