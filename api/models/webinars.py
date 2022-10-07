from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, Column, DateTime, Integer, String
from sqlalchemy.orm import Mapped, relationship

from ..settings import settings
from api.database import Base


if TYPE_CHECKING:
    from .webinar_participants import WebinarParticipant


class Webinar(Base):
    __tablename__ = "events_webinars"

    id: Mapped[str] = Column(String(36), primary_key=True, unique=True)
    skill_id: Mapped[str] = Column(String(256))
    creator: Mapped[str] = Column(String(36))
    creation_date: Mapped[datetime] = Column(DateTime)
    name: Mapped[str] = Column(String(256))
    description: Mapped[str] = Column(String(4096))
    link: Mapped[str] = Column(String(256))
    start: Mapped[datetime] = Column(DateTime)
    end: Mapped[datetime] = Column(DateTime)
    max_participants: Mapped[int] = Column(Integer)
    price: Mapped[int] = Column(BigInteger)
    participants: list[WebinarParticipant] = relationship(
        "WebinarParticipant", back_populates="webinar", lazy="selectin", cascade="all, delete-orphan"
    )

    def serialize(self, include_link: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "skill_id": self.skill_id,
            "creator": self.creator,
            "creation_date": self.creation_date.timestamp(),
            "ics_file": f"{settings.public_base_url.rstrip('/')}/webinars/{self.id}/webinar.ics",
            "name": self.name,
            "description": self.description,
            "link": self.link if include_link else None,
            "start": self.start.timestamp(),
            "end": self.end.timestamp(),
            "max_participants": self.max_participants,
            "price": self.price,
            "participants": len(self.participants),
        }
