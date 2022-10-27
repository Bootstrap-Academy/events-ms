"""Endpoints related to exams"""

import random
from datetime import timedelta
from typing import Any, AsyncIterator, Coroutine

from fastapi import APIRouter, Body, Query
from sqlalchemy import asc

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db, filter_by
from api.exceptions.auth import verified_responses
from api.exceptions.coaching import NotEnoughCoinsError
from api.exceptions.exams import ExamAlreadyBookedError, ExamAlreadyPassedError, ExamNotFoundError
from api.exceptions.skills import SkillNotFoundError, SkillRequirementsNotMetError
from api.models.slots import EventType
from api.schemas.exams import BookedExam, Exam, ExamSlot
from api.schemas.user import User
from api.services import shop
from api.services.auth import get_email, get_instructor
from api.services.skills import complete_skill, get_completed_skills, get_lecturers, get_skill_dependencies
from api.settings import settings
from api.utils.cache import clear_cache, redis_cached
from api.utils.email import BOOKED_EXAM
from api.utils.utc import utcnow


router = APIRouter()


async def get_examiners(skill_id: str) -> list[tuple[models.Exam, Coroutine[None, None, AsyncIterator[models.Slot]]]]:
    out = []
    for examiner in await get_lecturers({skill_id, settings.examiner_skill}):
        exam = await db.get(models.Exam, user_id=examiner, skill_id=skill_id)
        if not exam:
            continue

        out.append(
            (
                exam,
                db.stream(
                    filter_by(models.Slot, user_id=examiner, booked_by=None).where(
                        models.Slot.start - utcnow() >= timedelta(days=1)
                    )
                ),
            )
        )

    return out


@router.get("/exams", dependencies=[require_verified_email], responses=verified_responses(list[Exam]))
async def get_exams(user: User = user_auth) -> Any:
    """
    Return a list of all exams for an examiner.

    *Requirements:* **VERIFIED**
    """

    return [
        Exam(skill_id=exam.skill_id, price=settings.exam_price)
        async for exam in await db.stream(filter_by(models.Exam, user_id=user.id))
    ]


@router.get("/exams/{skill_id}", dependencies=[require_verified_email], responses=verified_responses(list[ExamSlot]))
@redis_cached("calendar", "skill_id")
async def get_slots(skill_id: str) -> Any:
    """
    Return a list of available times for an exam.

    *Requirements:* **VERIFIED**
    """

    out = []
    for exam, slots in await get_examiners(skill_id):
        async for slot in await slots:
            out.append(
                ExamSlot(
                    id=slot.id,
                    exam=Exam(skill_id=exam.skill_id, price=settings.exam_price),
                    start=slot.start.timestamp(),
                    end=slot.end.timestamp(),
                )
            )

    return out


@router.post(
    "/exams/{skill_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(
        ExamSlot,
        ExamNotFoundError,
        SkillNotFoundError,
        ExamAlreadyBookedError,
        ExamAlreadyPassedError,
        SkillRequirementsNotMetError,
        NotEnoughCoinsError,
    ),
)
async def book_exam(
    skill_id: str,
    start: int | None = Query(None, description="Start of the exam"),
    end: int | None = Query(None, description="End of the exam"),
    user: User = user_auth,
) -> Any:
    """
    Book an exam session.

    *Requirements:* **VERIFIED**
    """

    if await db.exists(filter_by(models.Slot, booked_by=user.id, event_type=EventType.EXAM, skill_id=skill_id)):
        raise ExamAlreadyBookedError

    if (dependencies := await get_skill_dependencies(skill_id)) is None:
        raise SkillNotFoundError

    completed_skills = await get_completed_skills(user.id)
    if skill_id in completed_skills:
        raise ExamAlreadyPassedError

    if not dependencies.issubset(completed_skills):
        raise SkillRequirementsNotMetError

    slots: list[models.Slot] = []
    for _, _slots in await get_examiners(skill_id):
        async for slot in await _slots:
            if (not start or start <= slot.start.timestamp()) and (not end or slot.end.timestamp() <= end):
                slots.append(slot)

    if not slots:
        raise ExamNotFoundError

    slot = random.choice(slots)  # noqa: S311

    if not await shop.spend_coins(user.id, settings.exam_price):
        raise NotEnoughCoinsError

    slot.book(
        user.id, EventType.EXAM, settings.exam_price, int(settings.exam_price * (1 - settings.event_fee)), skill_id
    )

    await clear_cache("calendar")

    if email := await get_email(user.id):
        await BOOKED_EXAM.send(
            email, datetime=slot.start.strftime("%d.%m.%Y %H:%M"), location=slot.meeting_link, coins=settings.exam_price
        )

    return ExamSlot(
        id=slot.id,
        exam=Exam(skill_id=skill_id, price=settings.exam_price),
        start=slot.start.timestamp(),
        end=slot.end.timestamp(),
    )


@router.get(
    "/exams/{skill_id}/pending", dependencies=[require_verified_email], responses=verified_responses(list[BookedExam])
)
async def get_pending_exams(skill_id: str, user: User = user_auth) -> Any:
    """
    Return a list of pending exams for an examiner.

    *Requirements:* **VERIFIED**
    """

    return [
        BookedExam(
            id=slot.id,
            exam=Exam(skill_id=slot.skill_id, price=settings.exam_price),
            start=slot.start.timestamp(),
            end=slot.end.timestamp(),
            student=await get_instructor(slot.booked_by),
        )
        async for slot in await db.stream(
            filter_by(models.Slot, user_id=user.id, event_type=EventType.EXAM, skill_id=skill_id)
            .where(models.Slot.booked_by != None)  # noqa
            .order_by(asc(models.Slot.start))
        )
    ]


@router.put(
    "/exams/{slot_id}/grade",
    dependencies=[require_verified_email],
    responses=verified_responses(bool, ExamNotFoundError),
)
async def grade_exam(
    *,
    passed: bool = Body(embed=True, description="Whether the user passed the exam"),
    slot_id: str,
    user: User = user_auth,
) -> Any:
    """
    Grade an exam.

    *Requirements:* **VERIFIED**
    """

    slot = await db.get(models.Slot, id=slot_id, user_id=user.id, event_type=EventType.EXAM)
    if not slot or not slot.booked_by or not slot.skill_id or not slot.instructor_coins or utcnow() < slot.start:
        raise ExamNotFoundError

    await db.delete(slot)
    if passed:
        await complete_skill(slot.booked_by, slot.skill_id)

    await shop.add_coins(user.id, slot.instructor_coins)

    await clear_cache("calendar")

    return True


@router.put(
    "/exams/{skill_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(Exam, SkillRequirementsNotMetError),
)
async def set_exam(skill_id: str, user: User = user_auth) -> Any:
    """
    Set up an exam for a skill.

    *Requirements:* **VERIFIED**
    """

    if not user.admin and not {settings.examiner_skill, skill_id}.issubset(await get_completed_skills(user.id)):
        raise SkillRequirementsNotMetError

    exam = await db.get(models.Exam, user_id=user.id, skill_id=skill_id)
    if not exam:
        await db.add(exam := models.Exam(user_id=user.id, skill_id=skill_id))

    await clear_cache("calendar")

    return Exam(skill_id=exam.skill_id, price=settings.exam_price)


@router.delete(
    "/exams/{skill_id}", dependencies=[require_verified_email], responses=verified_responses(bool, ExamNotFoundError)
)
async def delete_exam(skill_id: str, user: User = user_auth) -> Any:
    """
    Delete an exam for a skill.

    *Requirements:* **VERIFIED**
    """

    exam = await db.get(models.Exam, user_id=user.id, skill_id=skill_id)
    if not exam:
        raise ExamNotFoundError

    await db.delete(exam)

    await clear_cache("calendar")

    return True
