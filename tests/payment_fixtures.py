"""Explicit synthetic payment evidence for preexisting consumer tests."""

from typing import Any
from uuid import uuid4

from api.database import db
from api.models import BookingPayment, Slot, WebinarParticipant


def paid_participant(**kwargs: Any) -> WebinarParticipant:
    booking = WebinarParticipant(**kwargs)
    payment = BookingPayment(
        id=str(uuid4()),
        event_id=booking.webinar_id,
        user_id=booking.user_id,
        kind="webinar",
        state="paid",
        quoted_coins=booking.paid_coins,
        paid_coins=booking.paid_coins,
        description="Synthetic known payment",
        original={},
        evidence={"kind": "synthetic_fixture"},
    )
    booking.payment_id = payment.id
    db.session.add(payment)
    return booking


def paid_slot(**kwargs: Any) -> Slot:
    slot = Slot(**kwargs)
    if slot.booked_by is not None:
        payment = BookingPayment(
            id=str(uuid4()),
            event_id=slot.id,
            user_id=slot.booked_by,
            kind="coaching",
            state="paid",
            quoted_coins=slot.student_coins,
            paid_coins=slot.student_coins,
            payout_coins=slot.instructor_coins,
            description="Synthetic known payment",
            original={},
            evidence={"kind": "synthetic_fixture"},
        )
        slot.payment_id = payment.id
        db.session.add(payment)
    return slot
