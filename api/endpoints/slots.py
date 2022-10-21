from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body

from api import models
from api.auth import get_user, require_verified_email
from api.database import db, select
from api.exceptions.auth import admin_responses
from api.exceptions.slots import SlotBookedException, SlotNotFoundException
from api.schemas.slots import CreateSlot, Slot
from api.utils.cache import clear_cache


router = APIRouter()


@router.get("/slots/{user_id}", dependencies=[require_verified_email], responses=admin_responses(list[Slot]))
async def get_slots(user_id: str = get_user(require_self_or_admin=True)) -> Any:
    """
    Return the available slots for the user.

    *Requirements:* **VERIFIED** and (**SELF** or **ADMIN**)
    """

    return [slot.serialize async for slot in await db.stream(select(models.Slot).filter_by(user_id=user_id))]


@router.post("/slots/{user_id}", dependencies=[require_verified_email], responses=admin_responses(list[Slot]))
async def add_slots(
    slots: list[CreateSlot] = Body(embed=True), user_id: str = get_user(require_self_or_admin=True)
) -> Any:
    """
    Add slots for the user.

    *Requirements:* **VERIFIED** and (**SELF** or **ADMIN**)
    """

    await clear_cache("calendar")

    now = datetime.now().timestamp()
    return [
        (
            await models.Slot.create(
                user_id, datetime.fromtimestamp(slot.start), datetime.fromtimestamp(slot.start + 60 * slot.duration)
            )
        ).serialize
        for slot in slots
        if slot.start + 60 * slot.duration > now
    ]


@router.delete(
    "/slots/{user_id}/{slot_id}",
    dependencies=[require_verified_email],
    responses=admin_responses(bool, SlotNotFoundException, SlotBookedException),
)
async def delete_slot(slot_id: int, user_id: str = get_user(require_self_or_admin=True)) -> Any:
    """
    Delete a slot for the user.

    *Requirements:* **VERIFIED** and (**SELF** or **ADMIN**)
    """

    slot = await db.get(models.Slot, user_id=user_id, id=slot_id)
    if not slot:
        raise SlotNotFoundException

    if slot.booked:
        raise SlotBookedException

    await db.delete(slot)

    await clear_cache("calendar")

    return True


# todo: weekly slots
