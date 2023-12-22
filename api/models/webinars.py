from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Column, Integer, String
from sqlalchemy.orm import Mapped, relationship

from .emergency_cancel import EmergencyCancel
from .lecturer_rating import LecturerRating
from ..database.database import UTCDateTime
from ..schemas import calendar
from ..services import shop
from ..services.auth import get_userinfo
from ..services.skills import add_xp
from ..settings import settings
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
    admin_link: Mapped[str] = Column(String(256))
    link: Mapped[str] = Column(String(256))
    start: Mapped[datetime] = Column(UTCDateTime)
    end: Mapped[datetime] = Column(UTCDateTime)
    max_participants: Mapped[int] = Column(Integer)
    price: Mapped[int] = Column(BigInteger)
    participants: list[WebinarParticipant] = relationship(
        "WebinarParticipant", back_populates="webinar", lazy="selectin", cascade="all, delete-orphan"
    )

    async def serialize(self, include_link: bool, instructor: bool, booked: bool, bookable: bool) -> calendar.Webinar:
        return calendar.Webinar(
            id=self.id,
            skill_id=self.skill_id,
            instructor=await get_userinfo(self.creator),
            instructor_rating=await LecturerRating.get_rating(self.creator, self.skill_id),
            creation_date=int(self.creation_date.timestamp()),
            title=self.name,
            description=self.description,
            admin_link=self.admin_link if include_link and instructor else None,
            link=self.link if include_link else None,
            start=int(self.start.timestamp()),
            duration=int((self.end - self.start).total_seconds()) // 60,
            max_participants=self.max_participants,
            price=self.price,
            participants=len(self.participants),
            booked=booked,
            bookable=bookable,
        )


@db_wrapper
async def clean_old_webinars() -> None:
    webinar: Webinar
    async for webinar in await db.stream(select(Webinar, Webinar.participants).where(Webinar.end < utcnow())):
        for participant in webinar.participants:
            await LecturerRating.create(
                webinar.creator, participant.user_id, webinar.skill_id, webinar.start, webinar.name
            )
            await add_xp(participant.user_id, webinar.skill_id, settings.webinar_participant_xp)
        await shop.add_coins(
            webinar.creator, int(len(webinar.participants) * webinar.price * (1 - settings.event_fee)), "Webinar", True
        )
        if webinar.participants:
            await EmergencyCancel.delete(webinar.creator)
        await add_xp(webinar.creator, webinar.skill_id, settings.webinar_lecturer_xp)
        await db.delete(webinar)
