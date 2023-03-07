from api.services.internal import InternalService


async def add_coins(user_id: str, coins: int, description: str, credit_note: bool) -> bool:
    async with InternalService.SHOP.client as client:
        response = await client.post(
            f"/coins/{user_id}", json={"coins": coins, "description": description, "credit_note": credit_note}
        )
        return response.status_code == 200


async def spend_coins(user_id: str, coins: int, description: str) -> bool:
    return await add_coins(user_id, -coins, description, False)
