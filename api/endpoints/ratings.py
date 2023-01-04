"""Endpoints for rating lecturers."""

from typing import Any

from fastapi import APIRouter, Body

from api import models
from api.auth import get_user, require_verified_email, user_auth
from api.database import db
from api.exceptions.auth import admin_responses, verified_responses
from api.exceptions.ratings import CouldNotSendMessageError, RatingNotFoundError
from api.schemas.ratings import Unrated
from api.schemas.user import User
from api.services.auth import get_userinfo
from api.settings import settings
from api.utils.email import send_email


router = APIRouter()


@router.get(
    "/ratings/{user_id}/{skill_id}",
    dependencies=[require_verified_email],
    responses=admin_responses(float | None),  # type: ignore
)
async def get_rating(skill_id: str, user_id: str = get_user(require_self_or_admin=True)) -> Any:
    """Return the rating of a user for a skill."""

    return await models.LecturerRating.get_rating(user_id, skill_id)


@router.get("/unrated", dependencies=[require_verified_email], responses=verified_responses(list[Unrated]))
async def list_unrated(user: User = user_auth) -> Any:
    """Return a list of unrated webinars."""

    return [
        Unrated(
            id=x.id,
            instructor=await get_userinfo(x.lecturer_id),
            skill_id=x.skill_id,
            webinar_timestamp=x.webinar_timestamp.timestamp(),
            webinar_name=x.webinar_name,
        )
        for x in await models.LecturerRating.list_unrated(user.id)
    ]


@router.post(
    "/rate/{rating_id}", dependencies=[require_verified_email], responses=verified_responses(bool, RatingNotFoundError)
)
async def rate_lecturer(rating_id: str, rating: int = Body(embed=True, ge=1, le=5), user: User = user_auth) -> Any:
    """Rate a lecturer."""

    if not (r := await models.LecturerRating.get_unrated(user.id, rating_id)):
        raise RatingNotFoundError

    await r.add_rating(rating)
    return True


@router.post(
    "/rate/{rating_id}/report",
    dependencies=[require_verified_email],
    responses=verified_responses(bool, RatingNotFoundError, CouldNotSendMessageError),
)
async def report_lecturer(
    rating_id: str, reason: str = Body(embed=True, min_length=4, max_length=4096), user: User = user_auth
) -> Any:
    """Report a lecturer."""

    if not (r := await models.LecturerRating.get_unrated(user.id, rating_id)):
        raise RatingNotFoundError
    if not settings.contact_email:
        raise CouldNotSendMessageError

    if not (student := await get_userinfo(user.id)):
        raise CouldNotSendMessageError
    if not (lecturer := await get_userinfo(r.lecturer_id)):
        raise CouldNotSendMessageError

    try:
        await send_email(
            settings.contact_email,
            f"[Report] {student} reported {lecturer}",
            f"{student.display_name} ({student.name}, {student.email}) reported "
            f"{lecturer.display_name} ({lecturer.name}, {lecturer.email}) for the webinar {r.webinar_name} "
            f"(skill: {r.skill_id}) on {r.webinar_timestamp} (UTC): {reason}",
            reply_to=student.email,
        )
    except ValueError:
        raise CouldNotSendMessageError

    await db.delete(r)

    return True


@router.delete(
    "/rate/{rating_id}", dependencies=[require_verified_email], responses=verified_responses(bool, RatingNotFoundError)
)
async def cancel_rating(rating_id: str, user: User = user_auth) -> Any:
    """Rate a lecturer."""

    if not (r := await models.LecturerRating.get_unrated(user.id, rating_id)):
        raise RatingNotFoundError

    await db.delete(r)
    return True
