"""Endpoints related to the calendar."""

import hmac
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import Response

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db, select
from api.exceptions.auth import verified_responses
from api.schemas.user import User
from api.services.auth import is_admin
from api.services.ics import create_ics
from api.settings import settings
from api.utils.cache import redis_cached


router = APIRouter()


@router.get("/calendar", dependencies=[require_verified_email], responses=verified_responses(str))
async def get_calendar_url(
    booked_only: bool = Query(False, description="Only include booked webinars"), user: User = user_auth
) -> Any:
    """
    Return the private calendar url for the user.

    *Requirements:* **VERIFIED**
    """

    token = hmac.digest(settings.calendar_secret.encode(), user.id.encode(), "sha256").hex()
    token += str(int(booked_only))
    return f"{settings.public_base_url.rstrip('/')}/calendar/{user.id}/{token}/academy.ics"


@router.get("/calendar/{user_id}/{token}/academy.ics", include_in_schema=False)
@redis_cached("webinars", "user_id", "token")
async def download_ics(user_id: str, token: str) -> Any:
    if hmac.digest(settings.calendar_secret.encode(), user_id.encode(), "sha256").hex() != token[:-1]:
        return Response(status_code=401)

    admin = await is_admin(user_id)
    booked_only = token[-1] == "1"

    webinars = []
    async for webinar in await db.stream(select(models.Webinar)):
        booked = user_id == webinar.creator or any(
            participant.user_id == user_id for participant in webinar.participants
        )
        if booked_only and not booked:
            continue

        webinars.append((webinar, booked))

    return Response(create_ics(webinars, admin), media_type="text/calendar")
