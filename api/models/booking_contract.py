from typing import Any

from sqlalchemy import JSON, Boolean, Column, String
from sqlalchemy.orm import Mapped

from api.database import Base


class BookingContract(Base):
    __tablename__ = "events_booking_contracts"
    id: Mapped[str] = Column(String(36), primary_key=True)
    user_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    event_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    kind: Mapped[str] = Column(String(16), nullable=False)
    offer: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    acceptance: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)
    outcome: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)
    candidate: Mapped[dict[str, Any] | None] = Column(JSON(none_as_null=True), nullable=True)
    state: Mapped[str] = Column(String(24), nullable=False, index=True)
    closed: Mapped[bool] = Column(Boolean, nullable=False, default=False)
    fulfillment: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)
    reported: Mapped[bool] = Column(Boolean, nullable=False, default=False)


class BookingAvailability(Base):
    __tablename__ = "events_booking_availability"
    order_id: Mapped[str] = Column(String(36), primary_key=True)
    proof: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
