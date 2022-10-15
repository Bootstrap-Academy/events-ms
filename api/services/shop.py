from api.services.internal import InternalService


async def add_coins(user_id: str, coins: int) -> bool:
    async with InternalService.SHOP.client as client:
        response = await client.post(f"/coins/{user_id}", json={"coins": coins})
        return response.status_code == 200


async def spend_coins(user_id: str, coins: int) -> bool:
    return await add_coins(user_id, -coins)
