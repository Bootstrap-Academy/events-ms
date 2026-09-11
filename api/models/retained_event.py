"""Minimum existing booking rights; source evidence is separate from current access."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Column, ForeignKey, String, UniqueConstraint, false
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime


class RetainedEventRight(Base):
    __tablename__ = "events_retained_rights"
    __table_args__ = (UniqueConstraint("payment_id", "role"), {"mysql_collate": "utf8mb4_bin"})
    id: Mapped[str] = Column(String(36), primary_key=True)
    source_subject: Mapped[str] = Column(String(36), nullable=False, index=True)
    event_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    payment_id: Mapped[str] = Column(String(36), nullable=False)
    kind: Mapped[str] = Column(String(16), nullable=False)
    role: Mapped[str] = Column(String(16), nullable=False)
    observed_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    original: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    current_subject: Mapped[str | None] = Column(String(36), nullable=True, index=True)
    state: Mapped[str] = Column(String(24), nullable=False)


class RetainedEventErasure(Base):
    __tablename__ = "events_retained_right_erasures"
    id: Mapped[str] = Column(String(36), primary_key=True)
    right_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    subject: Mapped[str] = Column(String(36), nullable=False, index=True)
    observed_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    receipt: Mapped[dict[str, Any]] = Column(JSON, nullable=False)


class EventSubjectGuard(Base):
    __tablename__ = "events_subject_guards"
    subject: Mapped[str] = Column(String(36), primary_key=True)
    deleted: Mapped[bool] = Column(Boolean, nullable=False, default=False, server_default=false())
    booking_reservations: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)


class EventRightGrant(Base):
    __tablename__ = "events_right_grants"
    id: Mapped[str] = Column(String(36), primary_key=True)
    right_id: Mapped[str] = Column(String(36), ForeignKey("events_retained_rights.id"), nullable=False, index=True)
    subject: Mapped[str] = Column(String(36), nullable=False, index=True)
    state: Mapped[str] = Column(String(24), nullable=False)
    created_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    request: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    result: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
