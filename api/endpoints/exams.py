"""Endpoints related to exams"""
import random
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, Query

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
from api.services.calendly import EventType
from api.services.skills import complete_skill, get_completed_skills, get_lecturers, get_skill
from api.settings import settings


router = APIRouter()


async def get_examiners(skill_id: str) -> list[tuple[models.CalendlyLink, models.Exam, EventType, list[datetime]]]:
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

        out.append((link, exam, event_type, await calendly.fetch_available_slots(link.api_token, link.uri) or []))

    return out


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
    for (*_, event_type, slots) in await get_examiners(skill_id):
        for slot in slots:
            out.append(
                ExamSlot(price=settings.exam_price, start=slot, end=slot + timedelta(minutes=event_type.duration))
            )

    return out


@router.post(
    "/exams/{skill_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(
        str,
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
    start: datetime | None = Query(None, description="Start of the exam"),
    end: datetime | None = Query(None, description="End of the exam"),
    user: User = user_auth,
) -> Any:
    """
    Book an exam session.

    Note that the email address the user enters on the calendly page MUST match the email address of the user.
    Otherwise, the examiner cannot grade the exam.

    *Requirements:* **VERIFIED**
    """

    if row := await db.get(models.BookedExam, user_id=user.id, skill_id=skill_id):
        if not row.confirmed:
            return row.calendly_link

        raise ExamAlreadyBookedError

    if not (skill := await get_skill(skill_id)):
        raise SkillNotFoundError

    completed_skills = await get_completed_skills(user.id)
    if skill_id in completed_skills:
        raise ExamAlreadyPassedError

    if not skill.dependencies.issubset(completed_skills):
        raise SkillRequirementsNotMetError

    examiners: list[tuple[models.CalendlyLink, models.Exam]] = []
    for (link, exam, event_type, slots) in await get_examiners(skill_id):
        for slot in slots:
            if (not start or start <= slot) and (not end or slot + timedelta(minutes=event_type.duration) <= end):
                examiners.append((link, exam))
                break

    if not examiners:
        raise ExamNotFoundError

    link, exam = random.choice(examiners)  # noqa: S311

    if not await shop.spend_coins(user.id, settings.exam_price):
        raise NotEnoughCoinsError

    url = await calendly.create_single_use_link(link.api_token, link.uri)
    if not url:
        raise ExamNotFoundError

    await db.add(
        models.BookedExam(
            user_id=user.id, skill_id=skill_id, examiner_id=exam.user_id, confirmed=False, calendly_link=url
        )
    )

    return url


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

    if not user.admin and not {settings.examiner_skill, skill_id}.issubset(await get_completed_skills(user.id)):
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
