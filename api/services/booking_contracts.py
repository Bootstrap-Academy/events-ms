"""Original event contracts and documentary recovery, including no-charge bookings."""

import hashlib
import json
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, StrictBool

from api.database import db, db_wrapper, filter_by
from api.logger import get_logger
from api.models.booking_contract import BookingContract
from api.models.booking_payment import BookingPayment
from api.services import booking_availability
from api.services.auth import get_userinfo
from api.services.internal import InternalService
from api.utils.utc import utcnow


logger = get_logger(__name__)


class Acceptance(BaseModel):
    order_id: UUID
    offer_hash: str
    accepted: StrictBool
    early_performance_requested: StrictBool

    def payload(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.json()))


async def product(kind: str, event: Any, skill_id: str | None = None) -> dict[str, Any]:
    from api.models import Coaching, EmergencyCancel

    if kind == "webinar" and event.closed_to_new_bookings:
        raise HTTPException(409, "Webinar closed to new bookings")
    instructor_id = event.creator if kind == "webinar" else event.user_id
    instructor = await get_userinfo(instructor_id)
    if instructor is None:
        raise HTTPException(412, "Instructor unavailable")
    emergency = await EmergencyCancel.exists(instructor_id)
    if kind == "coaching":
        coaching = await db.get(Coaching, user_id=instructor_id, skill_id=skill_id)
        if coaching is None:
            raise HTTPException(404, "Coaching unavailable")
        price = coaching.price
    else:
        price = event.price
    facts = {
        "instructor_id": instructor_id,
        "instructor": instructor.display_name,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "timezone": "UTC",
        "skill_id": event.skill_id if kind == "webinar" else skill_id,
        "emergency_waiver": emergency,
        "automatic_renewal": False,
        "availability_protocol": booking_availability.PROTOCOL,
        "duration_minutes": int((event.end - event.start).total_seconds()) // 60,
    }
    title = event.name if kind == "webinar" else f"Coaching: {skill_id}"
    description = (
        f"{event.description if kind == 'webinar' else 'Einzel-Coaching'}\n"
        f"Durchführung: {instructor.display_name}.\n"
        f"Beginn: {facts['start']}; Ende: {facts['end']} (UTC).\n"
        "Anbieter und Vertragspartner: bootstrap academy GmbH. "
        "Die Zugangsdaten werden nach Vertragsbestätigung bereitgestellt."
    )
    commercial = {
        "kind": kind,
        "reference": event.id,
        "title": title,
        "description": description,
        "coins": 0 if emergency else price,
        "facts": facts,
        "service_starts_at": event.start.isoformat(),
    }
    commercial["revision"] = hashlib.sha256(
        json.dumps(commercial, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return commercial


async def offer(user_id: str, kind: str, event: Any, skill_id: str | None = None) -> dict[str, Any]:
    if not booking_availability.supported():
        raise HTTPException(503, "Prospective booking requires a supported committed-availability clock")
    current = await db.first(
        filter_by(BookingContract, user_id=user_id, event_id=event.id, kind=kind, closed=False).where(
            BookingContract.state.in_(["prepared", "confirmation_pending", "candidate", "ready", "review"])
        )
    )
    if current is not None:
        return {"offer": current.offer, "state": current.state}
    commercial = await product(kind, event, skill_id)
    async with InternalService.SHOP.client as client:
        response = await client.post(f"/purchase-offers/events/{user_id}", json=commercial)
        if response.status_code != 200:
            raise HTTPException(
                response.status_code if response.status_code in (409, 412) else 503, "Offer unavailable"
            )
        outcome = response.json()
    if outcome["state"] != "offered":
        return cast(dict[str, Any], outcome)
    await db.add(
        BookingContract(
            id=outcome["offer"]["id"],
            user_id=user_id,
            event_id=event.id,
            kind=kind,
            offer=outcome["offer"],
            state="offered",
            closed=False,
        )
    )
    return cast(dict[str, Any], outcome)


async def prepare(
    user_id: str, kind: str, event: Any, acceptance: Acceptance, skill_id: str | None = None
) -> BookingContract:
    contract = await db.first(
        filter_by(
            BookingContract, id=str(acceptance.order_id), user_id=user_id, event_id=event.id, kind=kind
        ).with_for_update()
    )
    if contract is None or contract.closed:
        raise HTTPException(404, "Offer unavailable")
    payload = acceptance.payload()
    if contract.acceptance is not None:
        if contract.acceptance != payload:
            raise HTTPException(409, "Conflicting order acceptance")
        return cast(BookingContract, contract)
    # An accepted emergency quote consumes one instructor-wide waiver. Lock
    # that row before revalidation, across distinct events/slots.
    if contract.offer["product"]["facts"]["emergency_waiver"]:
        from api.models import EmergencyCancel

        waiver = await db.first(
            filter_by(EmergencyCancel, user_id=event.creator if kind == "webinar" else event.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if waiver is None:
            raise HTTPException(409, "Emergency waiver already consumed; request a new offer")
    current = await product(kind, event, skill_id)
    original = dict(contract.offer["product"])
    # RFC3339 serializers may spell the same UTC offset differently.
    original["service_starts_at"] = current["service_starts_at"]
    if (
        original != current
        or contract.offer["hash"] != acceptance.offer_hash
        or not acceptance.accepted
        or (current["coins"] > 0 and not acceptance.early_performance_requested)
    ):
        raise HTTPException(409, "Offer changed or declarations missing; request a new offer")
    contract.acceptance = payload
    contract.state = "prepared"
    return cast(BookingContract, contract)


async def ready(payment: BookingPayment | None, *, continuation: bool = False) -> bool:
    if payment is None:
        return False
    contract = await db.get(BookingContract, id=payment.id)
    # No historical declaration/delivery is invented, and existing booked rights
    # are not removed by the prospective contract gate.
    if contract is None:
        return True
    if contract.closed or contract.state == "review":
        return False
    if contract.offer["product"]["facts"].get("availability_protocol") != booking_availability.PROTOCOL:
        return contract.state == "ready"
    usable, _, _, _ = (
        await booking_availability.read(payment.id, continuation=continuation)
        if continuation
        else await booking_availability.read(payment.id)
    )
    return usable


async def close(payment: BookingPayment | None) -> None:
    """Retain the old order while closing its reservation in the cancelling transaction."""
    if payment is not None:
        contract = await db.get(BookingContract, id=payment.id)
        if contract is not None:
            contract.closed = True


async def deliver(
    contract: BookingContract, payment: BookingPayment, booking_exists: bool, *, subject_erased: bool = False
) -> str:
    if subject_erased:
        # No new acceptance or availability after erasure. Retain an already
        # committed financial result without rewriting its original subject.
        async with InternalService.SHOP.client as client:
            response = await client.get(f"/purchase-status/{payment.user_id}/{contract.id}")
        if response.status_code == 200:
            outcome = response.json()
            contract.outcome = outcome
            return financial_result(contract, payment, outcome)
        return "pending"
    if contract.closed or not booking_exists or payment.original.get("student_deletion_claim"):
        contract.closed = True
        contract.state = "review"
        # A historical success remains evidence, never permission to charge anew.
        async with InternalService.SHOP.client as client:
            response = await client.get(f"/purchase-status/{payment.user_id}/{contract.id}")
        if response.status_code == 200:
            outcome = response.json()
            contract.outcome = outcome
            return financial_result(contract, payment, outcome)
        return "pending"
    async with InternalService.SHOP.client as client:
        response = await client.post(f"/purchases/events/{payment.user_id}", json=contract.acceptance)
    if response.status_code in (409, 412):
        contract.state = "failed"
        return "rejected"
    if response.status_code != 200:
        return "pending"
    outcome = response.json()
    contract.outcome = outcome
    financial = financial_result(contract, payment, outcome)
    if financial == "pending":
        return "pending"
    if outcome["state"] == "failed":
        contract.state = "failed"
        return "rejected"
    if outcome["state"] == "review":
        contract.state = "review"
        return financial
    if outcome["state"] in ("paid", "fulfilled"):
        sent = outcome.get("confirmation_smtp_accepted_at")
        start = contract.offer["product"]["facts"]["start"]
        from datetime import datetime

        if (
            sent
            and datetime.fromisoformat(sent.replace("Z", "+00:00")) < datetime.fromisoformat(start)
            and utcnow() < datetime.fromisoformat(start)
        ):
            contract.state = "candidate"
            candidate = {
                "kind": "booking_access_provided",
                "confirmation_smtp_accepted_at": outcome["confirmation_smtp_accepted_at"],
                "order_id": contract.id,
                "user_id": contract.user_id,
                "offer_hash": contract.offer["hash"],
                "event_id": contract.event_id,
                "event_kind": contract.kind,
                "scheduled_start": start,
                "scheduled_end": contract.offer["product"]["facts"]["end"],
                "session_performance": "not_observed",
                "paid_coins": payment.quoted_coins,
                "payout_ratio": payment.payout_ratio,
                "payout_coins": int(payment.quoted_coins * __import__("decimal").Decimal(payment.payout_ratio)),
                "ledger_id": outcome["financial_evidence"]["ledger_id"],
            }
            if contract.candidate is None:
                contract.candidate = candidate
        elif utcnow() >= datetime.fromisoformat(start):
            contract.state = "review"
        else:
            contract.state = "confirmation_pending"
        return "paid"
    return "pending"


async def after_commit(order_id: str) -> None:
    proof = await booking_availability.observe(order_id)
    contract = await db.first(
        filter_by(BookingContract, id=order_id).with_for_update().execution_options(populate_existing=True)
    )
    if contract is None or contract.candidate is None:
        return
    if proof is not None:
        if contract.fulfillment is None:
            contract.fulfillment = proof
        if not contract.closed and contract.state == "candidate":
            contract.state = "ready"
    else:
        usable, _, _, observed = await booking_availability.read(order_id)
        if (
            not usable
            and observed is not None
            and observed >= booking_availability.instant(contract.candidate["scheduled_start"])
        ):
            contract.state = "review"
    await db.commit()


async def report(contract: BookingContract) -> None:
    if contract.reported or contract.fulfillment is None:
        return
    async with InternalService.SHOP.client as client:
        response = await client.post(
            f"/purchase-fulfillment/events/{contract.user_id}/{contract.id}", json=contract.fulfillment
        )
    if response.status_code == 200:
        contract.reported = True
        await db.commit()


@db_wrapper
async def recover() -> None:
    from api.services.booking_payments import deliver as deliver_payment

    ids = [
        r.id
        for r in await db.all(
            filter_by(BookingContract).where(
                (
                    BookingContract.state.in_(["prepared", "confirmation_pending", "candidate"])
                    & (BookingContract.closed.is_(False))
                )
                | ((BookingContract.fulfillment.is_not(None)) & (BookingContract.reported.is_(False)))  # noqa: E712
            )
        )
    ]
    for order_id in ids:
        try:
            contract = await db.get(BookingContract, id=order_id)
            if contract is None:
                continue
            if contract.fulfillment is not None:
                await report(contract)
            else:
                await deliver_payment(order_id)
        except Exception:
            await db.session.rollback()
            logger.exception("Event confirmation recovery retained: %s", order_id)


async def customer_state(payment: BookingPayment | None) -> str | None:
    if payment is None:
        return None
    contract = await db.get(BookingContract, id=payment.id)
    if contract is not None and contract.state != "failed" and not await ready(payment):
        return "confirmation_pending" if contract.state != "review" else "review"
    return str(payment.state)


def financial_result(contract: BookingContract, payment: BookingPayment, outcome: dict[str, Any]) -> str:
    expected = contract.offer["product"]["coins"]
    if payment.quoted_coins != expected or outcome.get("offer", {}).get("hash") != contract.offer["hash"]:
        contract.state = "review"
        return "pending"
    if outcome.get("state") == "failed":
        return "rejected"
    proof = outcome.get("financial_evidence")
    if not isinstance(proof, dict) or proof.get("charged_coins") != expected:
        return "pending"
    if expected == 0 and (proof.get("no_charge") is not True or proof.get("ledger_id") is not None):
        return "pending"
    if expected > 0 and (proof.get("no_charge") is not False or proof.get("ledger_id") != contract.id):
        return "pending"
    return "paid"
