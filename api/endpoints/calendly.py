"""Endpoints related to the calendly integration"""

from typing import Any

from fastapi import APIRouter, Header, Request, Response

from api import models
from api.auth import get_user, require_verified_email
from api.database import db, filter_by
from api.exceptions.auth import admin_responses
from api.exceptions.calendly import (
    APITokenMissingError,
    CalendlyNotConfiguredError,
    EventTypeAmbiguousError,
    EventTypeNotFoundError,
    InvalidAPITokenError,
)
from api.schemas.calendly import SetupEventType, WebhookData
from api.services import calendly
from api.services.auth import get_user_id_by_email
from api.services.calendly import EventType


router = APIRouter()


@router.get(
    "/calendly/{user_id}",
    dependencies=[require_verified_email],
    responses=admin_responses(EventType, CalendlyNotConfiguredError),
)
async def get_configured_event_type(user_id: str = get_user(require_self_or_admin=True)) -> Any:
    """
    Return the configured calendly event type.

    *Requirements:* **VERIFIED** and (**SELF** or **ADMIN**)
    """

    if not (out := await calendly.get_calendly_link(user_id)):
        raise CalendlyNotConfiguredError

    return out


@router.patch(
    "/calendly/{user_id}",
    dependencies=[require_verified_email],
    responses=admin_responses(
        EventType, APITokenMissingError, InvalidAPITokenError, EventTypeNotFoundError, EventTypeAmbiguousError
    ),
)
async def configure_event_type(data: SetupEventType, user_id: str = get_user(require_self_or_admin=True)) -> Any:
    """
    Configure the calendly event type to use for coachings and exams.

    *Requirements:* **VERIFIED** and (**SELF** or **ADMIN**)
    """

    link = await db.get(models.CalendlyLink, user_id=user_id)
    if not link:
        if not data.api_token:
            raise APITokenMissingError

        await db.add(link := models.CalendlyLink.new(user_id, data.api_token))
    elif data.api_token:
        link.api_token = data.api_token

    calendly_user = await calendly.fetch_user(link.api_token)
    if not calendly_user:
        raise InvalidAPITokenError

    try:
        event_type = await calendly.find_event_type(link.api_token, calendly_user.uri, data.scheduling_url)
    except ValueError:
        raise EventTypeAmbiguousError

    if not event_type:
        raise EventTypeNotFoundError

    link.uri = event_type.uri

    await calendly.update_webhooks(calendly_user, link)

    return await calendly.update_calendly_link_cache(user_id, link)


@router.delete(
    "/calendly/{user_id}",
    dependencies=[require_verified_email],
    responses=admin_responses(bool, CalendlyNotConfiguredError),
)
async def delete_configured_event_type(user_id: str = get_user(require_self_or_admin=True)) -> Any:
    """
    Delete the configured calendly event type.

    *Requirements:* **VERIFIED** and (**SELF** or **ADMIN**)
    """

    link = await db.get(models.CalendlyLink, user_id=user_id)
    if not link:
        raise CalendlyNotConfiguredError

    if user := await calendly.fetch_user(link.api_token):
        await calendly.update_webhooks(user, link, delete=True)

    await db.delete(link)
    await calendly.clear_calendly_link_cache(user_id)

    return True


@router.post("/calendly/{user_id}/webhook", include_in_schema=False)
async def handle_webhook(
    data: WebhookData, user_id: str, request: Request, response: Response, calendly_webhook_signature: str = Header()
) -> Any:
    link = await db.get(models.CalendlyLink, user_id=user_id)
    if not link:
        response.status_code = 404
        return

    if not calendly.verify_webhook_signature(
        (await request.body()).decode(), calendly_webhook_signature, link.webhook_signing_key
    ):
        response.status_code = 401
        return

    if data.event != "invitee.created":
        return

    student_id = await get_user_id_by_email(data.payload.email)
    if not student_id:
        return

    # todo: find a way to get the skill_id of the exam
    async for booked_exam in await db.stream(filter_by(models.BookedExam, user_id=student_id)):
        booked_exam.confirmed = True
