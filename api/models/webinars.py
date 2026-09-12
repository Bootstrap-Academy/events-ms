from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, false
from sqlalchemy.orm import Mapped, relationship

from .emergency_cancel import EmergencyCancel
from .lecturer_rating import LecturerRating
from ..database.database import UTCDateTime
from ..schemas import calendar
from ..services import benefits, booking_contracts, booking_payments, commercial, payment_claims, settlements
from ..services.auth import get_userinfo
from ..settings import settings
from ..utils.utc import utcnow
from api.database import Base, db, db_wrapper, select
from api.models.booking_contract import BookingContract
from api.models.booking_payment import BookingPayment


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
    closed_to_new_bookings: Mapped[bool] = Column(Boolean, nullable=False, default=False, server_default=false())
    xp_delivery_protocol: Mapped[int | None] = Column(Integer, nullable=True)
    max_participants: Mapped[int] = Column(Integer)
    price: Mapped[int] = Column(BigInteger)
    participants: list[WebinarParticipant] = relationship(
        "WebinarParticipant", back_populates="webinar", lazy="selectin", cascade="all, delete-orphan"
    )

    async def serialize(self, include_link: bool, instructor: bool, booked: bool, bookable: bool) -> calendar.Webinar:
        return calendar.Webinar(
            id=self.id,
            type=calendar.EventType.WEBINAR,
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
    batch = await settlements.new_batch("payout")
    webinar: Webinar
    async for webinar in await db.stream(
        select(Webinar, Webinar.participants)
        .where(Webinar.end < utcnow())
        .with_for_update()
        .execution_options(populate_existing=True)
    ):
        ready_participants = []
        for participant in webinar.participants:
            payment = await booking_payments.payment_for(participant)
            if await commercial.cleanup_claims(payment, webinar, webinar.creator, batch.id):
                await booking_contracts.close(payment)
                continue
            if not await booking_contracts.ready(payment):
                contract = await db.get(BookingContract, id=payment.id)
                if contract is not None:
                    contract.state = "review"
                continue
            ready_participants.append(participant)
            await LecturerRating.create(
                webinar.creator, participant.user_id, webinar.skill_id, webinar.start, webinar.name
            )
            await benefits.record(webinar, payment, "participant", participant.user_id, settings.webinar_participant_xp)
        payments = [await booking_payments.payment_for(participant) for participant in ready_participants]
        # The current hosting subject is access authority. Original agreed
        # remuneration belongs to the original financial recipient.
        financial_groups: dict[str, list[BookingPayment]] = {}
        for payment in payments:
            owner = payment.original["commercial_event"]["instructor_id"]
            financial_groups.setdefault(owner, []).append(payment)
        for owner, group in sorted(financial_groups.items()):
            await payment_claims.credit(batch.id, webinar.id, owner, group, "Webinar", True, field="payout_coins")
        if ready_participants:
            await EmergencyCancel.delete(webinar.creator)
            await benefits.record(webinar, payments[0], "instructor", webinar.creator, settings.webinar_lecturer_xp)
        await db.delete(webinar)
    await settlements.finish([batch.id])
