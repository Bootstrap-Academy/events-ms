from typing import Any, cast

from api.schemas.user import User, UserInfo
from api.services.internal import InternalService
from api.utils.cache import redis_cached


async def exists_user_uncached(user_id: str) -> bool | None:
    """
    Check whether a user exists without using the cache.

    Returns `None` if the auth service responded with an unexpected status code.
    """

    async with InternalService.AUTH.client as client:
        response = await client.get(f"/users/{user_id}")
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        return None


@redis_cached("user", "user_id")
async def exists_user(user_id: str) -> bool:
    return await exists_user_uncached(user_id) is True


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


async def ordinary_authority(access_token: str, expected_user_id: str) -> User | None:
    """Uncached authority check; internal identity/existence keeps its own semantics.

    A request authorized before a restriction commits may finish. New requests
    require a currently enabled account and the durable matching backend session.
    """
    from fastapi import HTTPException
    from httpx import HTTPError
    from pydantic import ValidationError

    try:
        async with InternalService.AUTH.client as client:
            # A 401 here describes the forwarded ordinary token, not our
            # internal transport credential. Do not use the generic error hook.
            client.event_hooks["response"] = []
            response = await client.post("/ordinary-authority", json={"access_token": access_token})
        if response.status_code in (401, 403):
            return None
        if response.status_code != 200:
            raise HTTPException(503, "Account authority temporarily unavailable")
        payload = response.json()
        user = User.parse_obj({key: payload[key] for key in ("id", "email_verified", "admin")})
        if user.id != expected_user_id:
            raise HTTPException(503, "Invalid authority response")
        return user
    except (HTTPError, ValidationError, ValueError, KeyError, TypeError) as exc:
        from api.logger import get_logger

        get_logger(__name__).warning("Ordinary authority response unavailable (%s)", type(exc).__name__)
        raise HTTPException(503, "Account authority temporarily unavailable") from None
