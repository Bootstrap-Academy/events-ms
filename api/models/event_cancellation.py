"""Actual declarations and later claim evidence, independent of erasure receipts."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, ForeignKey, String
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime


class EventCancellation(Base):
    __tablename__ = "events_cancellation_declarations"
    id: Mapped[str] = Column(String(36), primary_key=True)
    source_subject: Mapped[str] = Column(String(36), nullable=False, index=True)
    right_id: Mapped[str] = Column(String(36), nullable=False)
    received_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    observed_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    original: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    result: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)


class EventCancellationClaimEvidence(Base):
    __tablename__ = "events_cancellation_claim_evidence"
    command_id: Mapped[str] = Column(String(36), ForeignKey("events_cancellation_declarations.id"), primary_key=True)
    claim_id: Mapped[str] = Column(String(36), ForeignKey("events_settlement_claims.id"), primary_key=True)
    observed_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    assessment: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
