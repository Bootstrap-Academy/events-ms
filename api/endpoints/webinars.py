"""Endpoints related to webinars."""

from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Query

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db, select
from api.exceptions.auth import PermissionDeniedError, verified_responses
from api.exceptions.coaching import NotEnoughCoinsError
from api.exceptions.skills import SkillRequirementsNotMetError
from api.exceptions.webinars import (
    AlreadyFullError,
    AlreadyRegisteredError,
    CannotStartInPastError,
    WebinarNotFoundError,
)
from api.schemas.user import User
from api.schemas.webinars import CreateWebinar, UpdateWebinar, Webinar
from api.services.shop import spend_coins
from api.services.skills import get_completed_skills
from api.settings import settings


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


@router.get("/webinars", dependencies=[require_verified_email], responses=verified_responses(list[Webinar]))
async def list_webinars(
    skill_id: str | None = Query(None, description="Filter by skill id"),
    creator: str | None = Query(None, description="Filter by creator id"),
    user: User = user_auth,
) -> Any:
    """
    Return a list of all webinars.

    The `link` is included iff the user has registered for the webinar, has created this webinar or is an admin.

    *Requirements:* **VERIFIED**
    """

    query = select(models.Webinar)
    if skill_id:
        query = query.filter_by(skill_id=skill_id)
    if creator:
        query = query.filter_by(creator=creator)

    return [
        webinar.serialize(
            user.admin
            or user.id == webinar.creator
            or any(participant.user_id == user.id for participant in webinar.participants)
        )
        async for webinar in await db.stream(query)
    ]


@router.post(
    "/webinars",
    dependencies=[require_verified_email],
    responses=verified_responses(Webinar, SkillRequirementsNotMetError, CannotStartInPastError),
)
async def create_webinar(data: CreateWebinar, user: User = user_auth) -> Any:
    """
    Create a new webinar.

    *Requirements:* **VERIFIED**
    """

    if not {settings.webinar_skill, data.skill_id}.issubset(await get_completed_skills(user.id)):
        raise SkillRequirementsNotMetError

    now = datetime.now()
    if data.start <= now.timestamp():
        raise CannotStartInPastError

    webinar = models.Webinar(
        id=str(uuid4()),
        skill_id=data.skill_id,
        creator=user.id,
        creation_date=now,
        name=data.name,
        description=data.description,
        link=data.link,
        start=datetime.fromtimestamp(data.start),
        end=datetime.fromtimestamp(data.start + data.duration * 60),
        max_participants=data.max_participants,
        price=data.price,
        participants=[],
    )
    await db.add(webinar)

    return webinar.serialize(True)


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
        bool, WebinarNotFoundError, AlreadyRegisteredError, AlreadyFullError, NotEnoughCoinsError
    ),
)
async def register_for_webinar(webinar: models.Webinar = get_webinar, user: User = user_auth) -> Any:
    """
    Register for a webinar.

    *Requirements:* **VERIFIED**
    """

    if user.id == webinar.creator or any(participant.user_id == user.id for participant in webinar.participants):
        raise AlreadyRegisteredError

    if len(webinar.participants) >= webinar.max_participants:
        raise AlreadyFullError

    if not await spend_coins(user.id, webinar.price):
        raise NotEnoughCoinsError

    webinar.participants.append(models.WebinarParticipant(user_id=user.id, webinar_id=webinar.id))

    return webinar.link


@router.patch(
    "/webinars/{webinar_id}",
    dependencies=[require_verified_email, can_manage_webinar],
    responses=verified_responses(Webinar, WebinarNotFoundError, PermissionDeniedError, CannotStartInPastError),
)
async def update_webinar(data: UpdateWebinar, webinar: models.Webinar = get_webinar) -> Any:
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
        if data.start <= datetime.now().timestamp():
            raise CannotStartInPastError
        webinar.end += datetime.fromtimestamp(data.start) - webinar.start
        webinar.start = datetime.fromtimestamp(data.start)

    if data.duration is not None:
        webinar.end = datetime.fromtimestamp(webinar.start.timestamp() + data.duration * 60)

    if data.max_participants is not None and data.max_participants != webinar.max_participants:
        webinar.max_participants = data.max_participants

    if data.price is not None and data.price != webinar.price:
        webinar.price = data.price

    return webinar.serialize(True)


@router.delete(
    "/webinars/{webinar_id}",
    dependencies=[require_verified_email, can_manage_webinar],
    responses=verified_responses(bool, WebinarNotFoundError, PermissionDeniedError),
)
async def delete_webinar(webinar: models.Webinar = get_webinar) -> Any:
    """
    Delete a webinar.

    Can only be accessed by the webinar host or an admin.

    *Requirements:* **VERIFIED**
    """

    await db.delete(webinar)

    return True
