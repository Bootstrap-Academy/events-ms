"""Safety net which removes the data of users that no longer exist."""

import asyncio
import time
from typing import Any

from httpx import HTTPError
from sqlalchemy import union
from sqlalchemy.sql import Select

from api.database import db, db_context, select
from api.logger import get_logger
from api.models import (
    CalendarToken,
    Coaching,
    EmergencyCancel,
    Exam,
    LecturerRating,
    Slot,
    Webinar,
    WebinarParticipant,
    WeeklySlot,
)
from api.services.auth import exists_user_uncached
from api.services.internal import InternalServiceError
from api.services.user_deletion import delete_user_data
from api.settings import settings


logger = get_logger(__name__)

# every column which contains the id of a user
USER_ID_COLUMNS: list[Any] = [
    Webinar.creator,
    WebinarParticipant.user_id,
    Slot.user_id,
    Slot.booked_by,
    WeeklySlot.user_id,
    Coaching.user_id,
    Exam.user_id,
    EmergencyCancel.user_id,
    LecturerRating.lecturer_id,
    LecturerRating.participant_id,
    CalendarToken.user_id,
]


class RateLimiter:
    """Rate limiter which spaces out a given number of operations per second evenly."""

    def __init__(self, rate: float) -> None:
        self._interval = 1 / rate if rate > 0 else 0.0
        self._next = 0.0

    async def acquire(self) -> None:
        now = time.monotonic()
        if (delay := self._next - now) > 0:
            await asyncio.sleep(delay)
        self._next = max(now, self._next) + self._interval


def user_id_batch_query(after: str | None, limit: int) -> Select:
    """Select the next batch of distinct user ids which are referenced anywhere in the database."""

    user_ids = union(*[select(column.label("user_id")) for column in USER_ID_COLUMNS]).subquery()
    query = select(user_ids.c.user_id).where(user_ids.c.user_id.is_not(None))
    if after is not None:
        query = query.where(user_ids.c.user_id > after)
    return query.order_by(user_ids.c.user_id).limit(limit)


async def sweep_deleted_users() -> None:
    """Delete the data of all users which the auth service does not know anymore."""

    rate_limiter = RateLimiter(settings.deleted_user_sweep_rate_limit)
    checked = missing = deleted = errors = 0
    after: str | None = None

    while True:
        async with db_context():
            user_ids: list[str] = await db.all(user_id_batch_query(after, settings.deleted_user_sweep_batch_size))
        if not user_ids:
            break
        after = user_ids[-1]

        for user_id in user_ids:
            await rate_limiter.acquire()
            checked += 1

            try:
                exists = await exists_user_uncached(user_id)
            except (InternalServiceError, HTTPError):
                errors += 1
                continue

            if exists is None:
                errors += 1
                continue
            if exists:
                continue

            missing += 1
            async with db_context():
                await delete_user_data(user_id)
            deleted += 1

    logger.info(
        "swept deleted users (checked: %s, missing: %s, deleted: %s, errors: %s)", checked, missing, deleted, errors
    )


def main() -> None:
    asyncio.run(sweep_deleted_users())
