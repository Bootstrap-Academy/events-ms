from typing import cast

from api.services.internal import InternalService


async def exists_user(user_id: str) -> bool:
    async with InternalService.AUTH.client as client:
        response = await client.get(f"/users/{user_id}")
        return response.status_code == 200


async def is_admin(user_id: str) -> bool:
    async with InternalService.AUTH.client as client:
        response = await client.get(f"/users/{user_id}")
        if response.status_code != 200:
            return False
        return cast(bool, response.json()["admin"])


async def get_user_id_by_email(email: str) -> str | None:
    async with InternalService.AUTH.client as client:
        response = await client.get(f"/users/by_email/{email}")
        if response.status_code != 200:
            return None

        return cast(str, response.json()["id"])
