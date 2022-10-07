"""Endpoints related to exams"""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Body

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db, filter_by
from api.exceptions.auth import verified_responses
from api.exceptions.calendly import CalendlyNotConfiguredError
from api.exceptions.coaching import NotEnoughCoinsError
from api.exceptions.exams import ExamAlreadyBookedError, ExamAlreadyPassedError, ExamNotFoundError
from api.exceptions.skills import SkillNotFoundError, SkillRequirementsNotMetError
from api.schemas.exams import BookedExam, Exam, ExamSlot
from api.schemas.user import User
from api.services import calendly, shop
from api.services.skills import complete_skill, exists_skill, get_completed_skills, get_lecturers
from api.settings import settings


router = APIRouter()


@router.get("/exams", dependencies=[require_verified_email], responses=verified_responses(list[Exam]))
async def get_exams(user: User = user_auth) -> Any:
    """
    Return a list of all exams for an examiner.

    *Requirements:* **VERIFIED**
    """

    return [
        Exam(examiner=exam.user_id, skill_id=exam.skill_id, price=settings.exam_price)
        async for exam in await db.stream(filter_by(models.Exam, user_id=user.id))
    ]


@router.get("/exams/{skill_id}", dependencies=[require_verified_email], responses=verified_responses(list[ExamSlot]))
async def get_available_times(skill_id: str) -> Any:
    """
    Return a list of available times for an exam.

    *Requirements:* **VERIFIED**
    """

    out = []
    for examiner in await get_lecturers({skill_id, settings.examiner_skill}):
        link = await db.get(models.CalendlyLink, user_id=examiner)
        if not link:
            continue

        exam = await db.get(models.Exam, user_id=examiner, skill_id=skill_id)
        if not exam:
            continue

        event_type = await calendly.fetch_event_type(link.api_token, link.uri)
        if not event_type:
            continue

        for slot in await calendly.fetch_available_slots(link.api_token, link.uri) or []:
            out.append(
                ExamSlot(
                    exam=Exam(examiner=examiner, skill_id=skill_id, price=settings.exam_price),
                    start=slot,
                    end=slot + timedelta(minutes=event_type.duration),
                )
            )

    return out


@router.post(
    "/exams/{skill_id}/{examiner}",
    dependencies=[require_verified_email],
    responses=verified_responses(
        str, ExamNotFoundError, SkillNotFoundError, ExamAlreadyBookedError, ExamAlreadyPassedError, NotEnoughCoinsError
    ),
)
async def book_exam(skill_id: str, examiner: str, user: User = user_auth) -> Any:
    """
    Book an exam session.

    Note that the email address the user enters on the calendly page MUST match the email address of the user.
    Otherwise, the examiner cannot grade the exam.

    *Requirements:* **VERIFIED**
    """

    exam = await db.get(models.Exam, user_id=examiner, skill_id=skill_id)
    if not exam:
        raise ExamNotFoundError

    link = await db.get(models.CalendlyLink, user_id=examiner)
    if not link:
        raise ExamNotFoundError

    if await db.exists(filter_by(models.BookedExam, user_id=user.id, skill_id=skill_id)):
        raise ExamAlreadyBookedError

    if not await exists_skill(skill_id):
        raise SkillNotFoundError

    if skill_id in await get_completed_skills(user.id):
        raise ExamAlreadyPassedError

    if not await shop.spend_coins(user.id, settings.exam_price):
        raise NotEnoughCoinsError

    await db.add(models.BookedExam(user_id=user.id, skill_id=skill_id, examiner_id=examiner, confirmed=False))

    return await calendly.create_single_use_link(link.api_token, link.uri)


@router.get(
    "/exams/{skill_id}/pending", dependencies=[require_verified_email], responses=verified_responses(list[BookedExam])
)
async def get_pending_exams(skill_id: str, user: User = user_auth) -> Any:
    """
    Return a list of pending exams for an examiner.

    *Requirements:* **VERIFIED**
    """

    return [
        BookedExam(user_id=exam.user_id, skill_id=exam.skill_id, examiner_id=exam.examiner_id)
        async for exam in await db.stream(
            filter_by(models.BookedExam, examiner_id=user.id, skill_id=skill_id, confirmed=True)
        )
    ]


@router.put(
    "/exams/{skill_id}/{user_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(bool, ExamNotFoundError),
)
async def grade_exam(
    *,
    passed: bool = Body(embed=True, description="Whether the user passed the exam"),
    skill_id: str,
    user_id: str,
    user: User = user_auth,
) -> Any:
    """
    Grade an exam.

    *Requirements:* **VERIFIED**
    """

    booked_exam = await db.get(
        models.BookedExam, user_id=user_id, skill_id=skill_id, examiner_id=user.id, confirmed=True
    )
    if not booked_exam:
        raise ExamNotFoundError

    await db.delete(booked_exam)
    if passed:
        await complete_skill(user_id, skill_id)

    return True


@router.put(
    "/exams/{skill_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(Exam, SkillRequirementsNotMetError, CalendlyNotConfiguredError),
)
async def set_exam(skill_id: str, user: User = user_auth) -> Any:
    """
    Set up an exam for a skill.

    *Requirements:* **VERIFIED**
    """

    if not {settings.examiner_skill, skill_id}.issubset(await get_completed_skills(user.id)):
        raise SkillRequirementsNotMetError

    if not await calendly.get_calendly_link(user.id):
        raise CalendlyNotConfiguredError

    exam = await db.get(models.Exam, user_id=user.id, skill_id=skill_id)
    if not exam:
        await db.add(exam := models.Exam(user_id=user.id, skill_id=skill_id))

    return Exam(examiner=exam.user_id, skill_id=exam.skill_id, price=settings.exam_price)


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

    return True
