"""Booking identities and payment evidence survive cancellation, rebooking and deletion."""

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, BigInteger, Column, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime
from api.utils.utc import utcnow


class BookingPayment(Base):
    __tablename__ = "events_booking_payments"

    id: Mapped[str] = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    event_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    user_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    kind: Mapped[str] = Column(String(16), nullable=False)
    state: Mapped[str] = Column(String(16), nullable=False, index=True)
    quoted_coins: Mapped[int | None] = Column(BigInteger, nullable=True)
    paid_coins: Mapped[int | None] = Column(BigInteger, nullable=True)
    payout_coins: Mapped[int | None] = Column(BigInteger, nullable=True)
    xp_delivery_protocol: Mapped[int | None] = Column(Integer, nullable=True)
    payout_ratio: Mapped[str | None] = Column(String(32), nullable=True)
    description: Mapped[str] = Column(String(4096), nullable=False)
    created_at: Mapped[datetime] = Column(UTCDateTime, nullable=False, default=utcnow)
    original: Mapped[dict[str, Any]] = Column(JSON, nullable=False, default=dict)
    evidence: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)
    ledger_transaction_id: Mapped[str | None] = Column(String(36), nullable=True, unique=True)
    attempts: Mapped[int] = Column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = Column(String(80), nullable=True)


class SettlementClaim(Base):
    __tablename__ = "events_settlement_claims"

    # This UUID becomes the T6 credit operation UUID once the amount is established.
    id: Mapped[str] = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    batch_id: Mapped[str] = Column(String(36), ForeignKey("events_settlement_batches.id"), nullable=False, index=True)
    event_id: Mapped[str] = Column(String(36), nullable=False)
    user_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    payment_ids: Mapped[list[str]] = Column(JSON, nullable=False)
    amount_field: Mapped[str] = Column(String(16), nullable=False)
    ratio: Mapped[str] = Column(String(32), nullable=False)
    description: Mapped[str] = Column(String(4096), nullable=False)
    credit_note: Mapped[bool] = Column(Integer, nullable=False)
    created_at: Mapped[datetime] = Column(UTCDateTime, nullable=False, default=utcnow)
    coins: Mapped[int | None] = Column(BigInteger, nullable=True)
    resolved_at: Mapped[datetime | None] = Column(UTCDateTime, nullable=True, index=True)
    entitlement: Mapped[str] = Column(String(32), nullable=False, default="established")
    basis: Mapped[dict[str, Any]] = Column(JSON, nullable=False, default=dict)


class CommercialHandoff(Base):
    __tablename__ = "events_commercial_handoffs"

    claim_id: Mapped[str] = Column(String(36), ForeignKey("events_settlement_claims.id"), primary_key=True)
    payload: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    receipt: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)
    acknowledged_at: Mapped[datetime | None] = Column(UTCDateTime, nullable=True)
    attempts: Mapped[int] = Column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = Column(String(80), nullable=True)


class CommercialBatchClaim(Base):
    __tablename__ = "events_commercial_batch_claims"
    batch_id: Mapped[str] = Column(String(36), ForeignKey("events_settlement_batches.id"), primary_key=True)
    claim_id: Mapped[str] = Column(String(36), ForeignKey("events_settlement_claims.id"), primary_key=True)


class CommercialErasureReceipt(Base):
    __tablename__ = "events_commercial_erasure_receipts"

    subject: Mapped[str] = Column(String(36), primary_key=True)
    observed_at: Mapped[datetime] = Column(UTCDateTime, nullable=False, default=utcnow)
    canonical: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)
    erased_at: Mapped[datetime | None] = Column(UTCDateTime, nullable=True)
    acknowledged_at: Mapped[datetime | None] = Column(UTCDateTime, nullable=True)
