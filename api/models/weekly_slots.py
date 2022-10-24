from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import Column, SmallInteger, String, Time
from sqlalchemy.orm import Mapped, relationship

from ..database.database import UTCDateTime
from ..utils.utc import utcnow
from api.database import Base, db


if TYPE_CHECKING:
    from . import Slot


class WeeklySlot(Base):
    __tablename__ = "events_weekly_slots"

    id: Mapped[str] = Column(String(36), primary_key=True, unique=True)
    user_id: Mapped[str] = Column(String(36))
    weekday: Mapped[int] = Column(SmallInteger)
    start: Mapped[time] = Column(Time)
    end: Mapped[time] = Column(Time)
    slots: list[Slot] = relationship("Slot", back_populates="weekly_slot", lazy="selectin")
    last_slot: Mapped[datetime] = Column(UTCDateTime)

    @property
    def serialize(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "weekday": self.weekday,
            "start": self.start.hour * 60 + self.start.minute,
            "end": self.end.hour * 60 + self.end.minute,
        }

    @classmethod
    async def create(cls, user_id: str, weekday: int, start: time, end: time) -> WeeklySlot:
        slot = cls(id=str(uuid4()), user_id=user_id, weekday=weekday, start=start, end=end, last_slot=utcnow())
        await db.add(slot)
        await slot.create_slots()
        return slot

    async def create_slots(self) -> None:
        from .slots import Slot

        until = utcnow() + timedelta(days=30)
        minutes = ((self.end.hour - self.start.hour) * 60 + (self.end.minute - self.start.minute)) % (60 * 24)
        while self.last_slot <= until:
            self.last_slot = next_slot(self.last_slot, self.weekday, self.start)
            slot = await Slot.create(self.user_id, self.last_slot, self.last_slot + timedelta(minutes=minutes))
            slot.weekly_slot = self
            slot.weekly_slot_id = self.id


def next_slot(start: datetime, weekday: int, t: time) -> datetime:
    if t <= start.time() and start.weekday() == weekday:
        start += timedelta(days=7)
    else:
        start += timedelta(days=(weekday - start.weekday()) % 7)
    return start.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
