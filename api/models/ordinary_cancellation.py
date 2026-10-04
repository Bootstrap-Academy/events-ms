"""Ordinary authenticated targets/declarations; never fabricated retained authority."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, ForeignKey, String
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime


class OrdinaryCancellationTarget(Base):
    __tablename__ = "events_ordinary_cancellation_targets"

    id: Mapped[str] = Column(String(36), primary_key=True)
    actor_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    prepared_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    original: Mapped[dict[str, Any]] = Column(JSON, nullable=False)


class OrdinaryEventCancellation(Base):
    __tablename__ = "events_ordinary_cancellations"

    id: Mapped[str] = Column(String(36), primary_key=True)
    actor_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    target_id: Mapped[str] = Column(String(36), ForeignKey("events_ordinary_cancellation_targets.id"), nullable=False)
    received_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    original: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    last_attempt_at: Mapped[datetime | None] = Column(UTCDateTime, nullable=True)
    result: Mapped[dict[str, Any] | None] = Column(JSON(none_as_null=True), nullable=True)


class OrdinaryCancellationClaimEvidence(Base):
    __tablename__ = "events_ordinary_cancellation_claim_evidence"

    command_id: Mapped[str] = Column(String(36), ForeignKey("events_ordinary_cancellations.id"), primary_key=True)
    claim_id: Mapped[str] = Column(String(36), ForeignKey("events_settlement_claims.id"), primary_key=True)
    observed_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    assessment: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
