"""Endpoints for rating lecturers."""

from typing import Any

from fastapi import APIRouter, Body

from api import models
from api.auth import get_user, require_verified_email, user_auth
from api.database import db
from api.exceptions.auth import admin_responses, verified_responses
from api.exceptions.ratings import RatingNotFoundError
from api.schemas.ratings import Unrated
from api.schemas.user import User
from api.services.auth import get_userinfo


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


@router.delete(
    "/rate/{rating_id}", dependencies=[require_verified_email], responses=verified_responses(bool, RatingNotFoundError)
)
async def cancel_rating(rating_id: str, user: User = user_auth) -> Any:
    """Rate a lecturer."""

    if not (r := await models.LecturerRating.get_unrated(user.id, rating_id)):
        raise RatingNotFoundError

    await db.delete(r)
    return True
