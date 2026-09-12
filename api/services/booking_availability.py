"""Committed access availability and later immutable evidence of that fact.

Before the accepted start a clean committed candidate is directly usable. The
witness is not an enabling write: it records that the candidate was already
usable when observed. After the start no timestamp made in the producer's
transaction can replace that evidence. All clocks here use the source database.
"""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import and_, case, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db
from api.models.booking_contract import BookingAvailability, BookingContract
from api.models.booking_payment import BookingPayment


PROTOCOL = "committed_candidate_v1"


def supported() -> bool:
    return db.engine.dialect.name in ("postgresql", "mysql")


def digest(candidate: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(candidate, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


async def read(
    order_id: str, *, continuation: bool = False
) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None, datetime | None]:
    from api.models import EventRightGrant, EventSubjectGuard, RetainedEventRight, Slot, Webinar, WebinarParticipant

    dialect = db.engine.dialect.name
    if not supported():
        return False, None, None, None
    # A new session guarantees no caller writes, autoflush, identity-map objects
    # or old repeatable-read snapshot can establish visibility. This SELECT is
    # deliberately nonlocking, including when a cleanup caller holds the event.
    if db.committed_read_engine is None:
        raise RuntimeError("Committed availability reader was not reserved at startup")
    # The bounded reserved pool runs only nonlocking reads and never calls back
    # into the owning pool. Saturated guard holders therefore cannot starve it.
    async with (
        asyncio.timeout(5),
        AsyncSession(db.committed_read_engine, autoflush=False, expire_on_commit=False) as session,
    ):
        row = (
            await session.execute(
                select(
                    BookingContract,
                    BookingPayment,
                    BookingAvailability,
                    Slot,
                    WebinarParticipant,
                    Webinar,
                    RetainedEventRight,
                    EventRightGrant,
                    EventSubjectGuard,
                )
                .join(BookingPayment, BookingPayment.id == BookingContract.id)
                .outerjoin(BookingAvailability, BookingAvailability.order_id == BookingContract.id)
                .outerjoin(Slot, (Slot.id == BookingContract.event_id) & (BookingContract.kind == "coaching"))
                .outerjoin(
                    WebinarParticipant,
                    (WebinarParticipant.payment_id == BookingContract.id) & (BookingContract.kind == "webinar"),
                )
                .outerjoin(Webinar, (Webinar.id == BookingContract.event_id) & (BookingContract.kind == "webinar"))
                .outerjoin(
                    RetainedEventRight,
                    and_(RetainedEventRight.payment_id == BookingContract.id, RetainedEventRight.role == "participant"),
                )
                .outerjoin(
                    EventRightGrant,
                    and_(
                        EventRightGrant.right_id == RetainedEventRight.id,
                        EventRightGrant.subject == RetainedEventRight.current_subject,
                        EventRightGrant.state == "granted",
                    ),
                )
                .outerjoin(
                    EventSubjectGuard,
                    EventSubjectGuard.subject
                    == case((BookingContract.kind == "coaching", Slot.booked_by), else_=WebinarParticipant.user_id),
                )
                .where(BookingContract.id == order_id)
            )
        ).first()
        # Sample AFTER the completed candidate read, never transaction-start time.
        clock = "SELECT clock_timestamp()" if dialect == "postgresql" else "SELECT UTC_TIMESTAMP(6)"
        now = (await session.execute(text(clock))).scalar_one()
        now = instant(now.isoformat())
        if row is None:
            return False, None, None, now
        contract, payment, witness, slot, participant, webinar, right, grant, guard = row
        candidate = json.loads(json.dumps(contract.candidate)) if contract.candidate else None
        proof = json.loads(json.dumps(witness.proof)) if witness else None
        if candidate is None:
            return False, None, proof, now
        current_subject = (
            slot.booked_by if contract.kind == "coaching" and slot else (participant.user_id if participant else None)
        )
        occupied = (
            slot is not None and bool(slot.link) and slot.payment_id == order_id
            if contract.kind == "coaching"
            else webinar is not None and bool(webinar.link) and participant is not None
        )
        exact_right = (
            right is not None
            and right.payment_id == order_id
            and right.event_id == contract.event_id
            and right.kind == contract.kind
            and right.original.get("start") == candidate["scheduled_start"]
            and right.original.get("end") == candidate["scheduled_end"]
            and right.original.get("payment_id") == order_id
        )
        exact_grant = (
            exact_right
            and right.state == "active"
            and right.current_subject == current_subject
            and grant is not None
            and grant.subject == current_subject
            and grant.request.get("original_scope", {}).get("id") == right.id
            and grant.request.get("original_scope", {}).get("original") == right.original
        )
        current_binding = (
            current_subject is not None
            and (guard is None or not guard.deleted)
            and (current_subject == contract.user_id or exact_grant)
        )
        # Only the exact independently authorized continuation caller can use a
        # preserved prior admission to move this same original right. Ordinary
        # reads require the current subject/binding and never revive old access.
        preserved_admission = (
            continuation
            and exact_right
            and right.state in ("preserved", "active")
            and right.original.get("admission_observed") is True
        )
        booking_exists = occupied and (current_binding or preserved_admission)
        eligible = (
            not contract.closed
            and contract.state in ("candidate", "ready")
            and booking_exists
            and not payment.original.get("student_deletion_claim")
            and payment.state in ("paid", "free")
            and candidate["order_id"] == order_id
            and candidate["user_id"] == contract.user_id
            and candidate["offer_hash"] == contract.offer["hash"]
            and candidate["paid_coins"] == payment.paid_coins == payment.quoted_coins
        )
        start = instant(candidate["scheduled_start"])
        timely_proof = (
            proof is not None
            and proof.get("timing_basis") == PROTOCOL
            and proof.get("candidate_hash") == digest(candidate)
            and proof.get("candidate") == candidate
            and instant(proof["availability_observed_at"]) < start
        )
        timely_preservation = (
            exact_right and right.original.get("admission_observed") is True and right.observed_at < start
        )
        return bool(eligible and (now < start or timely_proof or timely_preservation)), candidate, proof, now


async def observe(order_id: str) -> dict[str, Any] | None:
    usable, candidate, retained, observed_at = await read(order_id)
    if retained is not None:
        return retained
    if not usable or candidate is None or observed_at is None or observed_at >= instant(candidate["scheduled_start"]):
        return None
    proof = {
        **candidate,
        "provided_at": observed_at.isoformat(),
        "availability_observed_at": observed_at.isoformat(),
        "timing_basis": PROTOCOL,
        "candidate_hash": digest(candidate),
        "candidate": candidate,
    }
    # Append separately without acquiring event/payment/contract locks. Readers
    # before start remain able to use the candidate while this COMMIT is delayed.
    async with AsyncSession(db.engine, autoflush=False, expire_on_commit=False) as session:
        session.add(BookingAvailability(order_id=order_id, proof=proof))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
        saved = await session.get(BookingAvailability, order_id)
        if saved is None:
            raise RuntimeError("Availability observation was not retained")
        return cast(dict[str, Any], json.loads(json.dumps(saved.proof)))
