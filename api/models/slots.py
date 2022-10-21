from __future__ import annotations

import enum
import random
import string
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import BigInteger, Column, DateTime, Enum, String
from sqlalchemy.orm import Mapped

from api.database import Base, db, db_wrapper, select
from api.services import shop


class EventType(enum.Enum):
    COACHING = "coaching"
    EXAM = "exam"


class Slot(Base):
    __tablename__ = "events_slot"

    id: Mapped[str] = Column(String(36), primary_key=True, unique=True)
    user_id: Mapped[str] = Column(String(36))
    start: Mapped[datetime] = Column(DateTime)
    end: Mapped[datetime] = Column(DateTime)
    booked_by: Mapped[str | None] = Column(String(36), nullable=True)
    event_type: Mapped[EventType | None] = Column(Enum(EventType), nullable=True)
    student_coins: Mapped[int | None] = Column(BigInteger, nullable=True)
    instructor_coins: Mapped[int | None] = Column(BigInteger, nullable=True)
    skill_id: Mapped[str | None] = Column(String(256), nullable=True)
    meeting_link: Mapped[str | None] = Column(String(256), nullable=True)

    @property
    def booked(self) -> bool:
        return self.booked_by is not None

    @property
    def serialize(self) -> dict[str, Any]:
        return {"id": self.id, "start": self.start.timestamp(), "end": self.end.timestamp(), "booked": self.booked}

    @classmethod
    async def create(cls, user_id: str, start: datetime, end: datetime) -> Slot:
        slot = cls(
            id=str(uuid4()),
            user_id=user_id,
            start=start,
            end=end,
            booked_by=None,
            event_type=None,
            student_coins=None,
            instructor_coins=None,
            skill_id=None,
            meeting_link=None,
        )
        await db.add(slot)
        return slot

    def book(
        self, user_id: str, event_type: EventType, student_coins: int, instructor_coins: int, skill_id: str
    ) -> None:
        self.booked_by = user_id
        self.event_type = event_type
        self.student_coins = student_coins
        self.instructor_coins = instructor_coins
        self.skill_id = skill_id
        self.meeting_link = generate_meeting_link()

    def cancel(self) -> None:
        self.booked_by = None
        self.event_type = None
        self.student_coins = None
        self.instructor_coins = None
        self.skill_id = None
        self.meeting_link = None


def generate_meeting_link() -> str:
    return "https://meet.jit.si/" + "-".join(
        "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(4)) for _ in range(4)  # noqa: S311
    )


@db_wrapper
async def clean_old_slots() -> None:
    now = datetime.utcnow()
    slot: Slot
    async for slot in await db.stream(select(Slot).where(Slot.end < now)):
        if slot.booked and slot.event_type == EventType.EXAM and now - slot.end < timedelta(days=7):
            continue
        if slot.booked and slot.instructor_coins:
            await shop.add_coins(slot.user_id, slot.instructor_coins)
        await db.delete(slot)

    # todo: create from weekly slots (1 month)
