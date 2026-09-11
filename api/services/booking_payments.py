"""Durable keyed booking debits and payment provenance.

A committed reservation survives uncertain shop responses. Its immutable UUID is
replayed until acknowledgement; no unkeyed fallback or price recalculation occurs.
"""

from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from api.database import db, db_wrapper, filter_by
from api.exceptions.coaching import NotEnoughCoinsError
from api.logger import get_logger
from api.models.booking_payment import BookingPayment
from api.services import shop
from api.settings import settings


logger = get_logger(__name__)


def share(coins: int, ratio: str) -> int:
    return int(Decimal(coins) * Decimal(ratio))


def lecturer_ratio() -> str:
    return str(Decimal(1) - Decimal(str(settings.event_fee)))


async def reserve(
    kind: str, event_id: str, user_id: str, coins: int, description: str, emergency: bool, payment_id: str | None = None
) -> BookingPayment:
    if coins < 0:
        raise ValueError("Negative booking price")
    return await db.add(
        BookingPayment(
            id=payment_id or str(uuid4()),
            event_id=event_id,
            user_id=user_id,
            kind=kind,
            xp_delivery_protocol=1,  # prospective owning booking producer, separate from original contracts
            state="free" if coins == 0 else "pending",
            quoted_coins=coins,
            paid_coins=0 if coins == 0 else None,
            payout_coins=0 if coins == 0 else None,
            payout_ratio=lecturer_ratio(),
            description=description,
            original={"emergency_booking": emergency},
            evidence={"kind": "emergency_waiver" if emergency else "zero_price"} if coins == 0 else None,
        )
    )


async def finish(payment_id: str) -> None:
    await db.commit()  # reservation and emergency entitlement consumption before any remote effect
    state = await deliver(payment_id)
    if state == "failed":
        raise NotEnoughCoinsError
    if state == "pending":
        raise HTTPException(
            503, detail={"code": "EventBookingPaymentPending", "booking_reserved": True, "payment_id": payment_id}
        )


async def deliver(payment_id: str) -> str:
    from api.models import Slot, Webinar, WebinarParticipant

    payment = await db.get(BookingPayment, id=payment_id)
    if payment is None:
        raise ValueError("Booking payment is missing")
    from api.services import retained_events

    # New creation and erasure share this original subject fence. The financial
    # replay below still preserves exact prior results after a later erasure.
    guard = await retained_events.lock_subject(payment.user_id)
    # Owning parent remains before payment/contract locks.
    model = Webinar if payment.kind == "webinar" else Slot
    event = await db.first(
        filter_by(model, id=payment.event_id).with_for_update().execution_options(populate_existing=True)
    )
    payment = await db.first(
        filter_by(BookingPayment, id=payment_id).with_for_update().execution_options(populate_existing=True)
    )
    if payment is None:
        raise ValueError("Booking payment is missing")
    from api.models.booking_contract import BookingContract
    from api.services import booking_contracts

    contract = await db.get(BookingContract, id=payment.id)
    if payment.state != "pending" and (contract is None or contract.state in ("ready", "failed")):
        return str(payment.state)
    if payment.kind == "webinar":
        booking_exists = await db.get(WebinarParticipant, payment_id=payment.id) is not None
    else:
        booking_exists = event is not None and event.payment_id == payment.id and event.booked_by == payment.user_id
    try:
        if contract is not None:
            result = await booking_contracts.deliver(
                contract, payment, event is not None and booking_exists, subject_erased=guard.deleted
            )
        elif guard.deleted:
            result = "pending"  # absent old receipt is not permission for a new debit
        else:
            result = await shop.debit_booking(payment.id, payment.user_id, payment.quoted_coins, payment.description)
    except Exception as exc:
        result = "pending"
        payment.last_error = type(exc).__name__[:80]
    else:
        payment.last_error = "ShopResponseUncertain" if result == "pending" else None
    payment.attempts += 1
    if result != "pending":
        payment.state = ("free" if payment.quoted_coins == 0 else "paid") if result == "paid" else "failed"
        payment.paid_coins = payment.quoted_coins if result == "paid" else 0
        payment.payout_coins = share(payment.paid_coins, payment.payout_ratio)
        if contract is not None and result == "paid":
            assert contract.outcome is not None
        payment.evidence = {
            "kind": (
                ("purchase_contract" if contract is not None else "keyed_debit")
                if result == "paid"
                else "definitive_rejection"
            ),
            "operation_id": (
                (contract.outcome or {})["financial_evidence"]["ledger_id"]
                if contract is not None and result == "paid"
                else payment.id
            ),
        }
        if payment.kind == "webinar":
            booking = await db.get(WebinarParticipant, payment_id=payment.id)
            if booking is not None:
                if result == "paid":
                    booking.paid_coins = payment.paid_coins
                else:
                    await db.delete(booking)
        else:
            slot = await db.get(Slot, payment_id=payment.id)
            if slot is not None:
                if result == "paid":
                    slot.student_coins = payment.paid_coins
                    slot.instructor_coins = payment.payout_coins
                else:
                    slot.cancel()
    await db.commit()
    if contract is not None and contract.candidate is not None and contract.fulfillment is None:
        try:
            await booking_contracts.after_commit(payment_id)
        except Exception:
            await db.session.rollback()
            logger.exception("Committed booking availability remains uncertain: %s", payment_id)
        contract = await db.get(BookingContract, id=payment_id)
    if contract is not None and contract.fulfillment is not None:
        try:
            await booking_contracts.report(contract)
        except Exception:
            await db.session.rollback()
            logger.exception("Booking fulfillment report retained: %s", payment_id)
    return str(payment.state)


@db_wrapper
async def recover_booking_payments() -> None:
    ids = [p.id for p in await db.all(filter_by(BookingPayment, state="pending"))]
    for payment_id in ids:
        try:
            await deliver(payment_id)
        except Exception:
            await db.session.rollback()
            logger.exception("Booking payment recovery failed: %s", payment_id)
    counts = {state: await db.count(filter_by(BookingPayment, state=state)) for state in ("pending", "legacy_unknown")}
    if any(counts.values()):
        logger.warning("Booking payments require reconciliation: %s", counts)


async def payment_for(booking: Any) -> BookingPayment:
    payment = await db.get(BookingPayment, id=booking.payment_id) if booking.payment_id else None
    if payment is None:
        # An old/uninstrumented writer must never turn its asserted amount into payment proof.
        raise HTTPException(503, detail={"code": "EventBookingProvenanceMissing", "reconciliation_required": True})
    return payment
