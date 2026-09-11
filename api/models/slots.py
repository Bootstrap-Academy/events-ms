from __future__ import annotations

import enum
import random
import string
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import BigInteger, Column, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, relationship

from api.database import Base, db, db_wrapper, select
from api.database.database import UTCDateTime
from api.models.booking_contract import BookingContract
from api.models.weekly_slots import WeeklySlot
from api.services import benefits, booking_contracts, booking_payments, commercial, payment_claims, settlements
from api.settings import settings
from api.utils.utc import utcnow


class EventType(enum.Enum):
    COACHING = "coaching"
    EXAM = "exam"


class Slot(Base):
    __tablename__ = "events_slot"

    id: Mapped[str] = Column(String(36), primary_key=True, unique=True)
    user_id: Mapped[str] = Column(String(36))
    start: Mapped[datetime] = Column(UTCDateTime)
    end: Mapped[datetime] = Column(UTCDateTime)
    payment_id: Mapped[str | None] = Column(String(36), nullable=True, unique=True)
    booked_by: Mapped[str | None] = Column(String(36), nullable=True)
    event_type: Mapped[EventType | None] = Column(Enum(EventType), nullable=True)
    student_coins: Mapped[int | None] = Column(BigInteger, nullable=True)
    instructor_coins: Mapped[int | None] = Column(BigInteger, nullable=True)
    skill_id: Mapped[str | None] = Column(String(256), nullable=True)
    admin_link: Mapped[str | None] = Column(String(256), nullable=True)
    link: Mapped[str | None] = Column(String(256), nullable=True)
    weekly_slot_id: Mapped[str | None] = Column(String(36), ForeignKey("events_weekly_slots.id"), nullable=True)
    weekly_slot: Mapped[WeeklySlot | None] = relationship("WeeklySlot", back_populates="slots", lazy="selectin")

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
            admin_link=None,
            link=None,
            weekly_slot_id=None,
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
        self.admin_link, self.link = generate_meeting_link()

    def cancel(self) -> None:
        self.payment_id = None
        self.booked_by = None
        self.event_type = None
        self.student_coins = None
        self.instructor_coins = None
        self.skill_id = None
        self.admin_link = None
        self.link = None


def generate_meeting_link() -> tuple[str, str]:
    link = "https://meet.jit.si/" + "-".join(
        "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(4)) for _ in range(4)  # noqa: S311
    )
    return link, link


@db_wrapper
async def clean_old_slots() -> None:
    batch = await settlements.new_batch("payout")
    now = utcnow()
    slot: Slot
    async for slot in await db.stream(
        select(Slot).where(Slot.end < now).with_for_update().execution_options(populate_existing=True)
    ):
        # if slot.booked and slot.event_type == EventType.EXAM and now - slot.end < timedelta(days=7):
        #     continue
        if slot.booked and slot.booked_by is not None and slot.skill_id is not None:
            payment = None
            if slot.event_type == EventType.COACHING:
                payment = await booking_payments.payment_for(slot)
                if await commercial.cleanup_claims(payment, slot, slot.user_id, batch.id):
                    await booking_contracts.close(payment)
                    await db.delete(slot)
                    continue
                if not await booking_contracts.ready(payment):
                    contract = await db.get(BookingContract, id=payment.id)
                    if contract is not None:
                        contract.state = "review"
                    await db.delete(slot)
                    continue
                await payment_claims.credit(
                    batch.id,
                    slot.id,
                    payment.original["commercial_event"]["instructor_id"],
                    [payment],
                    "Coaching",
                    True,
                    field="payout_coins",
                )
            elif slot.instructor_coins:
                await settlements.credit(batch, slot.id, slot.user_id, slot.instructor_coins, "Coaching", True)
            await benefits.record(slot, payment, "instructor", slot.user_id, settings.coaching_lecturer_xp)
            await benefits.record(slot, payment, "participant", slot.booked_by, settings.coaching_participant_xp)
        await db.delete(slot)

    weekly_slot: WeeklySlot
    async for weekly_slot in await db.stream(select(WeeklySlot)):
        await weekly_slot.create_slots()
    await settlements.finish([batch.id])
