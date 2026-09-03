from typing import Any, cast

from api.schemas.user import UserInfo
from api.services.internal import InternalService
from api.utils.cache import redis_cached


@redis_cached("user", "user_id")
async def exists_user(user_id: str) -> bool:
    async with InternalService.AUTH.client as client:
        response = await client.get(f"/users/{user_id}")
        return response.status_code == 200


@redis_cached("user", "user_id")
async def get_email(user_id: str) -> str | None:
    async with InternalService.AUTH.client as client:
        response = await client.get(f"/users/{user_id}")
        if response.status_code != 200:
            return None
        return cast(str | None, response.json()["email"])


@redis_cached("user", "user_id")
async def is_admin(user_id: str) -> bool:
    async with InternalService.AUTH.client as client:
        response = await client.get(f"/users/{user_id}")
        if response.status_code != 200:
            return False
        return cast(bool, response.json()["admin"])


@redis_cached("user", "email")
async def get_user_id_by_email(email: str) -> str | None:
    async with InternalService.AUTH.client as client:
        response = await client.get(f"/users/by_email/{email}")
        if response.status_code != 200:
            return None

        return cast(str, response.json()["id"])


@redis_cached("user", "user_id")
async def _fetch_userinfo(user_id: str) -> dict[str, Any] | None:
    async with InternalService.AUTH.client as client:
        response = await client.get(f"/users/{user_id}")
        if response.status_code != 200:
            return None

        # only the fields UserInfo declares are cached, so no email address is written to redis here
        return UserInfo(**response.json()).dict()


async def get_userinfo(user_id: str) -> UserInfo | None:
    data = await _fetch_userinfo(user_id)
    return UserInfo(**data) if data is not None else None
