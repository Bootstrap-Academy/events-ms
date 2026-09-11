"""Durable cancellation/payout evidence. Never cascaded away with event or user data."""

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, BigInteger, Column, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime
from api.utils.utc import utcnow


class SettlementBatch(Base):
    __tablename__ = "events_settlement_batches"

    id: Mapped[str] = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    actor_id: Mapped[str | None] = Column(String(36), nullable=True, index=True)
    event_id: Mapped[str | None] = Column(String(36), nullable=True, index=True)
    kind: Mapped[str] = Column(String(20), nullable=False)
    created_at: Mapped[datetime] = Column(UTCDateTime, nullable=False, default=utcnow)
    notifications: Mapped[list[dict[str, Any]]] = Column(JSON, nullable=False, default=list)
    notified_at: Mapped[datetime | None] = Column(UTCDateTime, nullable=True)


class CoinOperation(Base):
    __tablename__ = "events_coin_operations"

    id: Mapped[str] = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    batch_id: Mapped[str] = Column(String(36), ForeignKey("events_settlement_batches.id"), nullable=False, index=True)
    event_id: Mapped[str] = Column(String(36), nullable=False)
    user_id: Mapped[str] = Column(String(36), nullable=False)
    coins: Mapped[int] = Column(BigInteger, nullable=False)
    description: Mapped[str] = Column(String(4096), nullable=False)
    credit_note: Mapped[bool] = Column(Integer, nullable=False)
    provenance: Mapped[str] = Column(String(24), nullable=False, default="ready")
    completed_at: Mapped[datetime | None] = Column(UTCDateTime, nullable=True, index=True)
    attempts: Mapped[int] = Column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = Column(String(80), nullable=True)
