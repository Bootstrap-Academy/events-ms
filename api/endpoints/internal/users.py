"""Internal endpoints related to users."""

from fastapi import APIRouter
from starlette.responses import Response

from api.schemas.user_export import UserDataExport
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data
from api.utils.docs import responses


router = APIRouter()


@router.get("/users/{user_id}/export", responses=responses(UserDataExport))
async def export_user(user_id: str) -> UserDataExport:
    """Return all data that belongs to a user. Returns empty lists if the user has no data in this service."""

    return await export_user_data(user_id)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str) -> Response:
    """Delete all data that belongs to a user. Does nothing if the user has no data in this service."""

    await delete_user_data(user_id)
    return Response(status_code=204)
