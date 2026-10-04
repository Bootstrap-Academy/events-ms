"""Prospective immutable XP earning requests and independent delivery observations."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime


class EventBenefit(Base):
    __tablename__ = "events_benefits"
    __table_args__ = (UniqueConstraint("source_kind", "source_id", "role"), {"mysql_collate": "utf8mb4_bin"})
    id: Mapped[str] = Column(String(36), primary_key=True)
    source_kind: Mapped[str] = Column(String(32), nullable=False)
    source_id: Mapped[str] = Column(String(36), nullable=False)
    role: Mapped[str] = Column(String(16), nullable=False)
    source_subject: Mapped[str] = Column(String(36), nullable=False, index=True)
    user_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    event_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    received_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    original: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    request: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    state: Mapped[str] = Column(String(24), nullable=False, default="pending")
    attempts: Mapped[int] = Column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    receipt: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)


class EventBenefitObservation(Base):
    __tablename__ = "events_benefit_observations"
    benefit_id: Mapped[str] = Column(String(36), ForeignKey("events_benefits.id"), primary_key=True)
    attempt: Mapped[int] = Column(Integer, primary_key=True)
    observed_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    result: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
