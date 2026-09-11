"""Endpoints related to 1-on-1 coachings"""

from datetime import timedelta
from typing import Any, cast

from fastapi import APIRouter, HTTPException

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db, filter_by
from api.exceptions.auth import verified_responses
from api.exceptions.coaching import CannotBookOwnCoachingError, CoachingNotFoundError, NotEnoughCoinsError
from api.exceptions.skills import SkillRequirementsNotMetError
from api.models.slots import EventType
from api.schemas import calendar
from api.schemas.coachings import Coaching, UpdateCoaching
from api.schemas.user import User
from api.services import booking_contracts, booking_payments, retained_events
from api.services.auth import get_userinfo
from api.services.skills import get_skill_levels
from api.settings import settings
from api.utils.cache import clear_cache
from api.utils.utc import utcnow


router = APIRouter()


@router.post(
    "/coachings/{skill_id}/{slot_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(
        calendar.Coaching, CoachingNotFoundError, NotEnoughCoinsError, CannotBookOwnCoachingError
    ),
)
async def book_coaching(data: booking_contracts.Acceptance, skill_id: str, slot_id: str, user: User = user_auth) -> Any:
    """
    Book a coaching session.

    *Requirements:* **VERIFIED**
    """

    await retained_events.require_current_subject(user.id)
    slot = await db.first(
        filter_by(models.Slot, id=slot_id).with_for_update().execution_options(populate_existing=True)
    )
    if slot and slot.booked_by == user.id and slot.payment_id:
        payment = await booking_payments.payment_for(slot)
        if str(data.order_id) == payment.id:
            await booking_contracts.prepare(user.id, "coaching", slot, data, skill_id)
            await booking_payments.finish(payment.id)
            return await _booked_coaching(slot)
    if not slot or slot.booked_by is not None or slot.start - utcnow() < timedelta(days=1):
        raise CoachingNotFoundError

    if slot.user_id == user.id:
        raise CannotBookOwnCoachingError

    coaching = await db.get(models.Coaching, user_id=slot.user_id, skill_id=skill_id)
    if not coaching:
        raise CoachingNotFoundError

    instructor = await get_userinfo(slot.user_id)
    if not instructor:
        raise CoachingNotFoundError

    contract = await booking_contracts.prepare(user.id, "coaching", slot, data, skill_id)
    emergency = bool(contract.offer["product"]["facts"]["emergency_waiver"])
    if emergency and not await models.EmergencyCancel.delete(slot.user_id):
        raise HTTPException(409, "Emergency waiver unavailable; no booking was placed")
    paid_coins = contract.offer["product"]["coins"]
    payment = await booking_payments.reserve(
        "coaching", slot.id, user.id, paid_coins, "Coaching", emergency, payment_id=contract.id
    )
    slot.book(user.id, EventType.COACHING, 0, 0, skill_id)
    slot.payment_id = payment.id
    slot.student_coins = payment.paid_coins
    slot.instructor_coins = payment.payout_coins
    await retained_events.record_booking_reservation(payment)
    await booking_payments.finish(payment.id)

    await clear_cache("calendar")

    return await _booked_coaching(slot)


async def _booked_coaching(slot: models.Slot) -> calendar.Coaching:
    payment = await booking_payments.payment_for(slot)
    return calendar.Coaching(
        id=slot.id,
        type=calendar.EventType.COACHING,
        title=None,
        description=None,
        skill_id=slot.skill_id,
        start=int(slot.start.timestamp()),
        duration=int((slot.end - slot.start).total_seconds()) // 60,
        price=slot.student_coins,
        payment_state=await booking_contracts.customer_state(payment),
        admin_link=None,
        link=slot.link if await booking_contracts.ready(payment) else None,
        instructor=await get_userinfo(slot.user_id),
        instructor_rating=await models.LecturerRating.get_rating(slot.user_id, slot.skill_id),
        booked=True,
        bookable=False,
        student=await get_userinfo(cast(str, slot.booked_by)),
    )


@router.get("/coachings", dependencies=[require_verified_email], responses=verified_responses(list[Coaching]))
async def get_coachings(user: User = user_auth) -> Any:
    """
    Return a list of all coaching configurations for an instructor.

    *Requirements:* **VERIFIED**
    """

    return [
        Coaching(skill_id=coaching.skill_id, price=coaching.price)
        async for coaching in await db.stream(filter_by(models.Coaching, user_id=user.id))
    ]


@router.put(
    "/coachings/{skill_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(Coaching, SkillRequirementsNotMetError),
)
async def set_coaching(data: UpdateCoaching, skill_id: str, user: User = user_auth) -> Any:
    """
    Set up a coaching for a skill.

    *Requirements:* **VERIFIED**
    """

    if not user.admin and (await get_skill_levels(user.id)).get(skill_id, 0) < settings.coaching_level:
        raise SkillRequirementsNotMetError

    coaching = await db.get(models.Coaching, user_id=user.id, skill_id=skill_id)
    if not coaching:
        await db.add(coaching := models.Coaching(user_id=user.id, skill_id=skill_id, price=data.price))
    else:
        coaching.price = data.price

    await clear_cache("calendar")

    return Coaching(skill_id=coaching.skill_id, price=coaching.price)


@router.delete(
    "/coachings/{skill_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(bool, CoachingNotFoundError),
)
async def delete_coaching(skill_id: str, user: User = user_auth) -> Any:
    """
    Delete a coaching for a skill.

    *Requirements:* **VERIFIED**
    """

    coaching = await db.get(models.Coaching, user_id=user.id, skill_id=skill_id)
    if not coaching:
        raise CoachingNotFoundError

    await db.delete(coaching)

    await clear_cache("calendar")

    return True


@router.post("/coachings/{skill_id}/{slot_id}/offer", dependencies=[require_verified_email])
async def coaching_offer(skill_id: str, slot_id: str, user: User = user_auth) -> Any:
    await retained_events.require_current_subject(user.id)
    slot = await db.first(
        filter_by(models.Slot, id=slot_id).with_for_update().execution_options(populate_existing=True)
    )
    if (
        slot is None
        or slot.booked_by is not None
        or slot.start - utcnow() < timedelta(days=1)
        or slot.user_id == user.id
    ):
        raise CoachingNotFoundError
    return await booking_contracts.offer(user.id, "coaching", slot, skill_id)
