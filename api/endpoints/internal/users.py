"""Internal endpoints related to users."""

from fastapi import APIRouter, HTTPException
from starlette.responses import Response

from api.schemas.user_export import UserDataExport
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data
from api.utils.docs import responses


router = APIRouter()


@router.post("/users/{user_id}/event-rights/{operation}")
async def event_rights(user_id: str, operation: str, body: dict) -> dict | list:
    from fastapi import HTTPException

    from api.services import retained_events

    if operation == "list" and body == {}:
        return await retained_events.list_rights(user_id)
    if operation == "original" and set(body) == {"right_id"} and isinstance(body["right_id"], str):
        return await retained_events.get_original(user_id, body["right_id"])
    if operation == "cancel" and set(body) == {"command_id"} and isinstance(body["command_id"], str):
        from api.services import event_cancellations

        return await event_cancellations.receive(user_id, body["command_id"])
    if operation == "deliver" and set(body) == {"grant_id"} and isinstance(body["grant_id"], str):
        return await retained_events.deliver(user_id, body["grant_id"])
    raise HTTPException(400, "Unsupported event-right operation")


@router.delete("/users/{user_id}/rights/events/{event_id}")
async def cancel_recipient_event(user_id: str, event_id: str) -> None:
    """An event ID/verified recipient alone supplies neither original order nor declaration.

    Retained callers must use their existing exact original-right declaration
    and command through event-rights/cancel; ordinary account authority is not
    fabricated from an internal service token. No new account is required.
    """
    raise HTTPException(
        409,
        detail={
            "code": "ExactCancellationTargetRequired",
            "cancellation_recorded": False,
            "retained_operation": "event-rights/cancel",
        },
    )


@router.get("/users/{user_id}/export", responses=responses(UserDataExport))
async def export_user(user_id: str) -> UserDataExport:
    """Return all data that belongs to a user. Returns empty lists if the user has no data in this service."""

    return await export_user_data(user_id)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str) -> Response:
    """Delete all data that belongs to a user. Does nothing if the user has no data in this service."""

    await delete_user_data(user_id)
    return Response(status_code=204)
