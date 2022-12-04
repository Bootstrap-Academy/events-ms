from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, Column, Integer, String
from sqlalchemy.orm import Mapped, relationship

from .lecturer_rating import LecturerRating
from ..database.database import UTCDateTime
from ..services.auth import get_instructor
from ..utils.utc import utcnow
from api.database import Base, db, db_wrapper, select


if TYPE_CHECKING:
    from .webinar_participants import WebinarParticipant


class Webinar(Base):
    __tablename__ = "events_webinars"

    id: Mapped[str] = Column(String(36), primary_key=True, unique=True)
    skill_id: Mapped[str] = Column(String(256))
    creator: Mapped[str] = Column(String(36))
    creation_date: Mapped[datetime] = Column(UTCDateTime)
    name: Mapped[str] = Column(String(256))
    description: Mapped[str] = Column(String(4096))
    link: Mapped[str] = Column(String(256))
    start: Mapped[datetime] = Column(UTCDateTime)
    end: Mapped[datetime] = Column(UTCDateTime)
    max_participants: Mapped[int] = Column(Integer)
    price: Mapped[int] = Column(BigInteger)
    participants: list[WebinarParticipant] = relationship(
        "WebinarParticipant", back_populates="webinar", lazy="selectin", cascade="all, delete-orphan"
    )

    async def serialize(self, include_link: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "skill_id": self.skill_id,
            "instructor": await get_instructor(self.creator),
            "rating": await LecturerRating.get_rating(self.creator, self.skill_id),
            "creation_date": self.creation_date.timestamp(),
            "name": self.name,
            "description": self.description,
            "link": self.link if include_link else None,
            "start": self.start.timestamp(),
            "end": self.end.timestamp(),
            "max_participants": self.max_participants,
            "price": self.price,
            "participants": len(self.participants),
        }


@db_wrapper
async def clean_old_webinars() -> None:
    webinar: Webinar
    async for webinar in await db.stream(select(Webinar, Webinar.participants).where(Webinar.end < utcnow())):
        for participant in webinar.participants:
            await LecturerRating.create(
                webinar.creator, participant.user_id, webinar.skill_id, webinar.start, webinar.name
            )
        await db.delete(webinar)
