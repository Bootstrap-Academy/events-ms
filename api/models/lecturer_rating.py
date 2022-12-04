from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import Mapped

from api.database import Base, db
from api.database.database import UTCDateTime, filter_by
from api.settings import settings
from api.utils.cache import clear_cache, redis_cached


class LecturerRating(Base):
    __tablename__ = "events_lecturer_rating"

    id: Mapped[str] = Column(String(36), primary_key=True, unique=True)
    lecturer_id: Mapped[str] = Column(String(36))
    participant_id: Mapped[str | None] = Column(String(36), nullable=True)
    skill_id: Mapped[str] = Column(String(256))
    webinar_timestamp: Mapped[datetime] = Column(UTCDateTime)
    webinar_name: Mapped[str | None] = Column(String(256), nullable=True)
    rating: Mapped[int | None] = Column(Integer, nullable=True)

    @classmethod
    async def create(
        cls, lecturer_id: str, participant_id: str, skill_id: str, webinar_timestamp: datetime, webinar_name: str
    ) -> LecturerRating:
        return await db.add(
            cls(
                id=str(uuid4()),
                lecturer_id=lecturer_id,
                participant_id=participant_id,
                skill_id=skill_id,
                webinar_timestamp=webinar_timestamp,
                webinar_name=webinar_name,
            )
        )

    async def add_rating(self, rating: int) -> None:
        self.rating = rating
        self.webinar_name = None
        self.participant_id = None
        await clear_cache("lecturer_rating")

    @classmethod
    async def list_unrated(cls, participant_id: str) -> list[LecturerRating]:
        return await db.all(filter_by(cls, participant_id=participant_id, rating=None))

    @classmethod
    async def get_unrated(cls, participant_id: str, rating_id: str) -> LecturerRating | None:
        return await db.get(cls, participant_id=participant_id, rating=None, id=rating_id)

    @classmethod
    @redis_cached("lecturer_rating", "lecturer_id", "skill_id")
    async def get_rating(cls, lecturer_id: str, skill_id: str) -> float | None:
        ratings = [
            (rating, rating.rating, rating.webinar_timestamp.timestamp())
            async for rating in await db.stream(
                filter_by(cls, lecturer_id=lecturer_id, skill_id=skill_id).where(cls.rating != None)  # noqa: E711
            )
        ]
        if not ratings:
            return None

        max_timestamp = max(ts for *_, ts in ratings)
        total = 0
        weights = 0
        for r, rating, timestamp in ratings:
            days = (max_timestamp - timestamp) / 3600 / 24
            if days > settings.rating_max_keep:
                await db.delete(r)
                continue
            weight = 2 ** (-days / settings.rating_half_life)
            total += rating * weight
            weights += weight
        return total / weights if weights else None
