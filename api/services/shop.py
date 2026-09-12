from typing import Any, Literal, overload

from api.services.internal import InternalService


@overload
async def commercial(
    operation: Literal["erasure", "register_event", "inventory", "event_cancellation_authority"], body: dict[str, Any]
) -> dict[str, Any] | None:
    pass


@overload
async def commercial(operation: Literal["event_cancellation_pending"], body: dict[str, Any]) -> list[Any]:
    pass


@overload
async def commercial(operation: Literal["event_cancellation_outcome"], body: dict[str, Any]) -> dict[str, Any]:
    pass


@overload
async def commercial(operation: str, body: dict[str, Any]) -> dict[str, Any] | list[Any] | None:
    pass


async def commercial(operation: str, body: dict[str, Any]) -> dict[str, Any] | list[Any] | None:
    if operation not in {
        "erasure",
        "register_event",
        "inventory",
        "event_cancellation_authority",
        "event_cancellation_pending",
        "event_cancellation_outcome",
    }:
        raise ValueError("Unsupported commercial operation")
    async with InternalService.SHOP.client as client:
        response = await client.post(f"/claims/{operation}", json=body)
        response.raise_for_status()
        result: object = response.json()
        if operation == "event_cancellation_pending":
            if not isinstance(result, list):
                raise ValueError("Malformed cancellation inventory")
        elif operation == "event_cancellation_outcome":
            if not isinstance(result, dict):
                raise ValueError("Malformed cancellation outcome receipt")
        elif result is not None and not isinstance(result, dict):
            raise ValueError("Malformed commercial receipt")
        return result


async def add_coins(user_id: str, coins: int, description: str, credit_note: bool) -> bool:
    async with InternalService.SHOP.client as client:
        response = await client.post(
            f"/coins/{user_id}", json={"coins": coins, "description": description, "credit_note": credit_note}
        )
        return response.status_code == 200


async def apply_coin_operation(
    operation_id: str, user_id: str, coins: int, description: str, credit_note: bool
) -> bool:
    # A separate route deliberately fails closed against an old backend which cannot
    # deduplicate. Never fall back to the unkeyed POST after an error or timeout.
    async with InternalService.SHOP.client as client:
        response = await client.put(
            f"/coin-operations/{operation_id}/{user_id}",
            json={"coins": coins, "description": description, "credit_note": credit_note},
        )
        if response.status_code != 200:
            return False
        balance = response.json()
        return isinstance(balance, dict) and all(
            isinstance(balance.get(field), int) and not isinstance(balance[field], bool) and balance[field] >= 0
            for field in ["coins", "withheld_coins"]
        )


async def spend_coins(user_id: str, coins: int, description: str) -> bool:
    return await add_coins(user_id, -coins, description, False)


async def debit_booking(operation_id: str, user_id: str, coins: int, description: str) -> str:
    async with InternalService.SHOP.client as client:
        response = await client.put(
            f"/coin-operations/{operation_id}/{user_id}",
            json={"coins": -coins, "description": description, "credit_note": False},
        )
        # A known rejection proves no debit was committed for this key. All other
        # errors, malformed responses and transport failures remain uncertain.
        if response.status_code == 412 and response.json() == {"detail": "Not enough coins"}:
            return "rejected"
        if response.status_code != 200:
            return "pending"
        balance = response.json()
        valid = isinstance(balance, dict) and all(
            isinstance(balance.get(field), int) and not isinstance(balance[field], bool) and balance[field] >= 0
            for field in ["coins", "withheld_coins"]
        )
        return "paid" if valid else "pending"
