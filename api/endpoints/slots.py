from datetime import datetime, time
from typing import Any

from fastapi import APIRouter, Body

from api import models
from api.auth import get_user, require_verified_email
from api.database import db, filter_by
from api.exceptions.auth import admin_responses
from api.exceptions.slots import SlotBookedException, SlotNotFoundException
from api.schemas.slots import CreateSlot, CreateWeeklySlot, Slot, WeeklySlot
from api.utils.cache import clear_cache


router = APIRouter()


@router.get("/slots/{user_id}", dependencies=[require_verified_email], responses=admin_responses(list[Slot]))
async def get_slots(user_id: str = get_user(require_self_or_admin=True)) -> Any:
    """
    Return the available slots for the user.

    *Requirements:* **VERIFIED** and (**SELF** or **ADMIN**)
    """

    return [slot.serialize async for slot in await db.stream(filter_by(models.Slot, user_id=user_id))]


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
async def delete_slot(slot_id: str, user_id: str = get_user(require_self_or_admin=True)) -> Any:
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


@router.get(
    "/slots/{user_id}/weekly", dependencies=[require_verified_email], responses=admin_responses(list[WeeklySlot])
)
async def get_weekly_slots(user_id: str = get_user(require_self_or_admin=True)) -> Any:
    """
    Return the rules for creating slots on a weekly basis for the user.

    *Requirements:* **VERIFIED** and (**SELF** or **ADMIN**)
    """

    slot: models.WeeklySlot
    return [slot.serialize for slot in await db.all(filter_by(models.WeeklySlot, user_id=user_id))]


@router.post("/slots/{user_id}/weekly", dependencies=[require_verified_email], responses=admin_responses(WeeklySlot))
async def add_weekly_slot(data: CreateWeeklySlot, user_id: str = get_user(require_self_or_admin=True)) -> Any:
    """
    Add a rule for creating slots on a weekly basis for the user.

    *Requirements:* **VERIFIED** and (**SELF** or **ADMIN**)
    """

    return (
        await models.WeeklySlot.create(
            user_id, data.weekday, time(*divmod(data.start, 60)), time(*divmod(data.end, 60))
        )
    ).serialize


@router.delete(
    "/slots/{user_id}/weekly/{slot_id}",
    dependencies=[require_verified_email],
    responses=admin_responses(bool, SlotNotFoundException),
)
async def delete_weekly_slot(slot_id: str, user_id: str = get_user(require_self_or_admin=True)) -> Any:
    """
    Delete a rule for creating slots on a weekly basis for the user.

    *Requirements:* **VERIFIED** and (**SELF** or **ADMIN**)
    """

    slot = await db.get(models.WeeklySlot, user_id=user_id, id=slot_id)
    if not slot:
        raise SlotNotFoundException

    booked = False
    for s in [*slot.slots]:
        if s.booked:
            booked = True
            s.weekly_slot = None
            s.weekly_slot_id = None
        else:
            await db.delete(s)

    await db.delete(slot)

    if booked:
        await clear_cache("calendar")

    return True
