"""Exact ordinary declarations, immutable receipts and separately assessed returns."""

from datetime import datetime, timezone
import json
from sqlalchemy import func
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from api.database import db, db_context, filter_by, select
from api.logger import get_logger
from api.models import BookingPayment, EmergencyCancel, EventRightGrant, Slot, Webinar, WebinarParticipant
from api.models.booking_contract import BookingContract
from api.models.booking_payment import CommercialBatchClaim, SettlementClaim
from api.models.ordinary_cancellation import (
    OrdinaryCancellationClaimEvidence,
    OrdinaryCancellationTarget,
    OrdinaryEventCancellation,
)
from api.models.settlement import CoinOperation, SettlementBatch
from api.schemas.ordinary_cancellation import CancellationDeclaration, CancellationPreparation
from api.schemas.user import User
from api.services import booking_contracts, event_cancellations, payment_claims, retained_events, settlements
from api.utils.cache import clear_cache
from api.utils.email import COMMERCIAL_CANCELLATION
from api.utils.utc import utcnow


ORIGIN = "ordinary_authenticated"
logger = get_logger(__name__)


def declaration_body(body: CancellationDeclaration) -> dict[str, Any]:
    return body.dict() | {"target_id": str(body.target_id)}


def received_instant(receipt: OrdinaryEventCancellation) -> datetime:
    return datetime.fromisoformat(receipt.original["received_at"].replace("Z", "+00:00")).astimezone(timezone.utc)


def target_view(target_id: str, target: dict[str, Any]) -> dict[str, Any]:
    """Owner-visible scope; other participants' identities/individual amounts stay private."""
    amounts = [order["paid_coins"] for order in target["orders"]]
    return {
        "id": target_id,
        **{key: target[key] for key in ("event_id", "kind", "scope", "role", "title", "start", "end")},
        "affected_orders": len(amounts),
        "recorded_paid_coins": sum(amounts) if all(amount is not None for amount in amounts) else None,
        "payment_evidence_complete": all(order["payment_id"] is not None for order in target["orders"]),
    }


async def locked_event(kind: str, event_id: str) -> Any:
    return await db.first(
        filter_by(Webinar if kind == "webinar" else Slot, id=event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def occupancy(event: Any, kind: str) -> list[Any]:
    if kind == "webinar":
        return await db.all(
            filter_by(WebinarParticipant, webinar_id=event.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    return [event] if event.booked_by is not None else []


def recipient(booking: Any, kind: str) -> str:
    return booking.user_id if kind == "webinar" else booking.booked_by


def current_provider(event: Any, kind: str) -> str:
    return event.creator if kind == "webinar" else event.user_id


async def payment_inventory(ids: list[str]) -> dict[str, BookingPayment]:
    payments = {}
    # IDs came from actual occupied rows or the durable accepted target. Original
    # financial IDs are retained; do not probe an arbitrary client-supplied range.
    for payment_id in sorted(set(ids)):
        payment = await db.first(
            filter_by(BookingPayment, id=payment_id).with_for_update().execution_options(populate_existing=True)
        )
        if payment is not None:
            payments[payment_id] = payment
    return payments


async def prepare(user: User, event_id: str, request: CancellationPreparation) -> dict[str, Any]:
    await retained_events.require_current_subject(user.id)
    event = await locked_event(request.kind, event_id)
    if event is None:
        raise HTTPException(404, "Event unavailable")
    bookings = await occupancy(event, request.kind)
    if request.kind == "coaching" and not bookings:
        raise HTTPException(404, "No occupied coaching booking is available")
    mine = [row for row in bookings if recipient(row, request.kind) == user.id]
    provider = current_provider(event, request.kind)
    if request.scope != "session" and mine:
        role, scope, chosen = "participant", "booking", mine
    elif request.scope != "booking" and provider == user.id:
        role, scope, chosen = "provider", "session", bookings
    elif request.scope == "session" and user.admin:
        role, scope, chosen = "administrator", "session", bookings
    elif request.scope == "auto" and user.admin:
        role, scope, chosen = "administrator", "session", bookings
    else:
        raise HTTPException(403, "This cancellation scope is not yours")
    payments = await payment_inventory([row.payment_id for row in chosen if row.payment_id])
    orders = []
    for booking in chosen:
        payment = payments.get(booking.payment_id)
        if payment is not None and (payment.event_id != event_id or payment.kind != request.kind):
            raise HTTPException(409, "Original booking evidence requires resolution")
        facts = payment.original.get("commercial_event", {}) if payment is not None else {}
        if not facts and payment is not None:
            contract = await db.get(BookingContract, id=payment.id)
            facts = contract.offer.get("product", {}).get("facts", {}) if contract else {}
        orders.append(
            {
                "payment_id": payment.id if payment else None,
                "recipient": recipient(booking, request.kind),
                "payer": payment.user_id if payment else None,
                "instructor": facts.get("instructor_id", provider),
                "paid_coins": payment.paid_coins if payment else None,
            }
        )
    target = {
        "actor_id": user.id,
        "event_id": event_id,
        "kind": request.kind,
        "scope": scope,
        "role": role,
        "provider": provider,
        "title": event.name if request.kind == "webinar" else event.skill_id or "Coaching",
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "orders": sorted(orders, key=lambda order: (order["payment_id"] or "", order["recipient"])),
    }
    row = await db.add(
        OrdinaryCancellationTarget(id=str(uuid4()), actor_id=user.id, prepared_at=utcnow(), original=target)
    )
    await db.commit()
    return target_view(row.id, target)


async def owned_receipt(actor: str, command: str, *, lock: bool = False) -> OrdinaryEventCancellation | None:
    keys = await payment_claims.committed_and_local_keys(
        select(OrdinaryEventCancellation.id).where(OrdinaryEventCancellation.id == command)
    )
    if not keys:
        return None
    query = filter_by(OrdinaryEventCancellation, id=command).execution_options(populate_existing=True)
    row = await db.first(query.with_for_update() if lock else query)
    if row is None or row.actor_id != actor:
        raise HTTPException(404, "Cancellation receipt unavailable")
    return row


async def receive(
    user: User, command: str, body: CancellationDeclaration, *, received_at: datetime | None = None
) -> dict[str, Any]:
    observed = (received_at or utcnow()).astimezone(timezone.utc)
    await retained_events.lock_subject(user.id)
    receipt = await owned_receipt(user.id, command, lock=True)
    submitted = declaration_body(body)
    if receipt is not None:
        if receipt.original["declaration"] != submitted:
            raise HTTPException(409, "This receipt belongs to a different declaration")
        if receipt.result is not None:
            return await receipt_view(receipt)
    else:
        target = await db.get(OrdinaryCancellationTarget, id=str(body.target_id))
        if target is None or target.actor_id != user.id:
            raise HTTPException(404, "Prepared cancellation scope unavailable")
        if target.original["role"] == "administrator" and (not user.admin or not body.administration_reason):
            raise HTTPException(403, "Administrative intervention requires current authority and its reason")
        if target.original["role"] != "administrator" and body.administration_reason is not None:
            raise HTTPException(409, "An administration reason does not change this prepared role")
        receipt = await db.add(
            OrdinaryEventCancellation(
                id=command,
                actor_id=user.id,
                target_id=target.id,
                received_at=observed,
                original={
                    "origin": ORIGIN,
                    "actor_id": user.id,
                    "authenticated_admin": user.admin,
                    "received_at": observed.isoformat(),
                    "target": target.original,
                    "declaration": submitted,
                },
                result=None,
            )
        )
    # Declaration time/scope survive any later application, contact or handoff
    # failure. Merely opening a preparation is never a cancellation receipt.
    await db.commit()
    try:
        await apply(user, command)
    except Exception:
        logger.exception("Accepted ordinary cancellation application remains pending")
        await db.session.rollback()
    saved = await owned_receipt(user.id, command)
    assert saved is not None
    return await receipt_view(saved)


async def status(user: User, command: str) -> dict[str, Any]:
    receipt = await owned_receipt(user.id, command)
    if receipt is None:
        raise HTTPException(404, "Cancellation receipt unavailable")
    return await receipt_view(receipt)


async def resolved(receipt: OrdinaryEventCancellation, reason: str) -> None:
    receipt.result = {"state": "resolution_required", "reason": reason, "booking_changed": False, "claim_ids": []}
    await db.commit()


async def apply(user: User, command: str) -> None:
    # The caller freshly authenticated, but application resumes the already
    # accepted declaration; it cannot enlarge or replace its original authority.
    await apply_saved(user.id, command)


async def apply_saved(actor_id: str, command: str) -> None:
    await retained_events.lock_subject(actor_id)
    receipt = await owned_receipt(actor_id, command, lock=True)
    if receipt is None or receipt.result is not None:
        return
    target = receipt.original["target"]
    if (
        receipt.original.get("origin") != ORIGIN
        or receipt.original.get("actor_id") != actor_id
        or target.get("actor_id") != actor_id
        or (target["role"] == "administrator" and receipt.original.get("authenticated_admin") is not True)
    ):
        raise ValueError("Original accepted ordinary authority is unavailable")
    event = await locked_event(target["kind"], target["event_id"])
    detached = event is None
    chosen = []
    if not detached:
        if (event.start.isoformat(), event.end.isoformat()) != (target["start"], target["end"]):
            await resolved(receipt, "event_period_changed")
            return
        if current_provider(event, target["kind"]) != target["provider"]:
            await resolved(receipt, "provider_changed")
            return
        current = await occupancy(event, target["kind"])
        chosen = (
            current
            if target["scope"] == "session"
            else [row for row in current if recipient(row, target["kind"]) == actor_id]
        )
        expected = {(order["payment_id"], order["recipient"]) for order in target["orders"]}
        if {(row.payment_id, recipient(row, target["kind"])) for row in chosen} != expected:
            await resolved(receipt, "booking_membership_changed")
            return
    if any(order["payment_id"] is None for order in target["orders"]):
        await resolved(receipt, "original_payment_missing")
        return
    payments = await payment_inventory([order["payment_id"] for order in target["orders"]])
    if any(
        order["payment_id"] not in payments
        or payments[order["payment_id"]].event_id != target["event_id"]
        or payments[order["payment_id"]].kind != target["kind"]
        or payments[order["payment_id"]].user_id != order["payer"]
        for order in target["orders"]
    ):
        await resolved(receipt, "original_payment_changed")
        return
    received = received_instant(receipt)
    start = datetime.fromisoformat(target["start"])
    batch = await settlements.new_batch("cancellation", actor_id, target["event_id"])
    batch.created_at = received
    basis = {
        "ordinary_cancellation_command_id": receipt.id,
        "receipt_source": ORIGIN,
        "request_kind": "identified_service_cancellation",
        "request_received_at": received.isoformat(),
        "actor_role": target["role"],
        "scope": target["scope"],
        "cancellation_by": target["role"],
        "cancellation_inferred_from_erasure": False,
        "event_start": target["start"],
    }
    observations = []
    for order in target["orders"]:
        payment = payments[order["payment_id"]]
        if target["role"] == "participant":
            await payment_claims.cancel_student(
                batch.id,
                target["event_id"],
                payment.user_id,
                order["instructor"],
                payment,
                start,
                received,
                basis,
                observations,
            )
        else:
            await payment_claims.credit(
                batch.id,
                target["event_id"],
                payment.user_id,
                [payment],
                "Cancelled event booking",
                False,
                entitlement="established" if received <= start else "pending_evidence",
                basis=basis
                | {"assessment": "service_not_provided" if received <= start else "actual_performance_review"},
                observations=observations,
            )
            if received > start:
                await payment_claims.credit(
                    batch.id,
                    target["event_id"],
                    order["instructor"],
                    [payment],
                    "Event remuneration requires assessment",
                    True,
                    field="payout_coins",
                    entitlement="pending_evidence",
                    basis=basis | {"assessment": "actual_performance_review"},
                    observations=observations,
                )
        if detached:
            continue
        await booking_contracts.close(payment)
        for role in ("participant", "instructor"):
            right = await retained_events.right_for(payment.id, role)
            if right is not None:
                right.state, right.current_subject = "cancelled", None
                for grant in await db.all(
                    filter_by(EventRightGrant, right_id=right.id, state="granted")
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ):
                    grant.state = "withdrawn"
    await record_assessments(receipt.id, observations)
    if not detached:
        if target["role"] == "provider" and chosen:
            await EmergencyCancel.create(actor_id)
        if target["kind"] == "webinar":
            # Whole-session membership was compared before any parent delete can
            # cascade; the parent lock keeps newly joining orders out until commit.
            if target["scope"] == "session":
                await db.delete(event)
            else:
                for row in chosen:
                    await db.delete(row)
        else:
            event.cancel()
    links = await db.all(filter_by(CommercialBatchClaim, batch_id=batch.id))
    receipt.result = {
        "state": "resolution_required" if detached else "applied",
        "reason": "event_unavailable_original_orders_assessed" if detached else None,
        "booking_changed": not detached,
        "batch_id": batch.id,
        "claim_ids": sorted(link.claim_id for link in links),
    }
    for subject in {actor_id, target["provider"]} | {order["recipient"] for order in target["orders"]}:
        notice = target_view(receipt.target_id, target) | {
            "command_id": receipt.id,
            "received_at": received.isoformat(),
            "state": "resolution_required" if detached else "applied",
            "actor_is_recipient": actor_id == subject,
        }
        settlements.notification(batch, COMMERCIAL_CANCELLATION, subject, ordinary_cancellation=notice)
    await db.commit()
    # These retries have no authority to select another booking or rewrite the
    # immutable result. No contact/cache/financial-service call precedes commit.
    try:
        await clear_cache("calendar")
    except Exception:
        pass
    try:
        await settlements.finish([batch.id])
    except Exception:
        await db.session.rollback()


async def receipt_view(receipt: OrdinaryEventCancellation) -> dict[str, Any]:
    result = receipt.result or {"state": "received", "booking_changed": False, "claim_ids": []}
    claims = await db.all(select(SettlementClaim).where(SettlementClaim.id.in_(result["claim_ids"])))
    operations = await db.all(select(CoinOperation).where(CoinOperation.id.in_(result["claim_ids"])))
    financial = "not_assessed"
    if claims:
        if any(claim.coins is None for claim in claims):
            financial = "amount_unknown"
        elif any(claim.entitlement != "established" for claim in claims):
            financial = "requires_review"
        elif any(op.provenance != "ready" or op.last_error for op in operations):
            financial = "uncertain"
        elif all(claim.coins == 0 for claim in claims):
            financial = "recorded_zero"
        elif operations and all(op.completed_at is not None for op in operations):
            financial = "historical_application"
        else:
            financial = "pending"
    batch = await db.get(SettlementBatch, id=result["batch_id"]) if result.get("batch_id") else None
    return {
        "command_id": receipt.id,
        "origin": ORIGIN,
        "received_at": received_instant(receipt).isoformat(),
        "target": target_view(receipt.target_id, receipt.original["target"]),
        "declaration": receipt.original["declaration"],
        "state": result["state"],
        "reason": result.get("reason"),
        "booking_changed": result["booking_changed"],
        "financial_state": financial,
        "financial_satisfaction": False,
        "notice_state": "smtp_accepted" if batch is not None and batch.notified_at is not None else "pending",
    }


async def record_assessments(
    command: str, observations: list[tuple[Any, list[BookingPayment], str, dict[str, Any]]]
) -> None:
    # Whole-session calls may encounter one original aggregate through several
    # orders or roles. Assemble its complete observation before immutable insert.
    groups = {}
    for claim, payments, entitlement, basis in observations:
        owner, components = groups.setdefault(claim.id, (claim, {}))
        key = json.dumps([entitlement, basis], sort_keys=True)
        component = components.setdefault(key, {"payment_ids": [], "entitlement": entitlement, "basis": basis})
        component["payment_ids"] = sorted(set(component["payment_ids"]) | {payment.id for payment in payments})
    for claim, components in groups.values():
        await observe_components(claim, command, [components[key] for key in sorted(components)])


async def observe_claim(
    claim: Any, command: str, payments: list[BookingPayment], entitlement: str, basis: dict[str, Any]
) -> None:
    await observe_components(
        claim,
        command,
        [{"payment_ids": sorted(payment.id for payment in payments), "entitlement": entitlement, "basis": basis}],
    )


async def observe_components(claim: Any, command: str, components: list[dict[str, Any]]) -> None:
    receipt = await db.get(OrdinaryEventCancellation, id=command)
    payment_ids = {payment_id for component in components for payment_id in component["payment_ids"]}
    if (
        receipt is None
        or receipt.original.get("origin") != ORIGIN
        or not payment_ids <= {order["payment_id"] for order in receipt.original["target"]["orders"]}
    ):
        raise ValueError("Ordinary claim assessment requires its exact authenticated declaration")
    assessment = (
        components[0] if len(components) == 1 else {"payment_ids": sorted(payment_ids), "components": components}
    ) | {"original_receipt_time": received_instant(receipt).isoformat()}
    existing = next((row for row in await claim_evidence(claim.id, current=True) if row["command_id"] == command), None)
    if existing is not None:
        if existing["assessment"] != assessment:
            raise ValueError("Conflicting original ordinary cancellation assessment")
        return
    await db.add(
        OrdinaryCancellationClaimEvidence(
            command_id=command, claim_id=claim.id, observed_at=utcnow(), assessment=assessment
        )
    )
    await event_cancellations.qualify_claim(claim)


async def claim_evidence(claim_id: str, *, current: bool = False) -> list[dict[str, Any]]:
    if current:
        query = select(OrdinaryCancellationClaimEvidence.command_id).add_columns(
            OrdinaryCancellationClaimEvidence.claim_id
        )
        keys = await payment_claims.committed_and_local_keys(
            query.where(OrdinaryCancellationClaimEvidence.claim_id == claim_id)
        )
        rows = []
        for command, owner_claim in sorted(keys):
            row = await db.first(
                filter_by(OrdinaryCancellationClaimEvidence, command_id=command, claim_id=owner_claim)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None:
                raise ValueError("Original ordinary claim evidence disappeared")
            rows.append(row)
    else:
        rows = await db.all(filter_by(OrdinaryCancellationClaimEvidence, claim_id=claim_id))
    return [
        {
            "origin": ORIGIN,
            "command_id": row.command_id,
            "observed_at": row.observed_at.isoformat(),
            "assessment": row.assessment,
        }
        for row in rows
    ]


async def export(subject: str) -> dict[str, Any]:
    return {
        "targets": [
            target_view(row.id, row.original) | {"prepared_at": row.prepared_at}
            for row in await db.all(filter_by(OrdinaryCancellationTarget, actor_id=subject))
        ],
        "declarations": [
            await receipt_view(row) for row in await db.all(filter_by(OrdinaryEventCancellation, actor_id=subject))
        ],
    }


async def recover() -> None:
    """Resume at most ten accepted declarations in fresh owning sessions.

    This is processing of the saved authenticated declaration, not a new ordinary
    login or a retained claimant request. Acceptance authority/time stay original;
    exact current scope checks can resolve a changed target without touching it.
    """
    async with db_context():
        rows = await db.all(
            select(OrdinaryEventCancellation)
            .where(OrdinaryEventCancellation.result.is_(None))
            .order_by(
                func.coalesce(OrdinaryEventCancellation.last_attempt_at, OrdinaryEventCancellation.received_at),
                OrdinaryEventCancellation.id,
            )
            .limit(10)
        )
        pending = [(row.actor_id, row.id) for row in rows]
    for actor_id, command in pending:
        try:
            async with db_context():
                await retained_events.lock_subject(actor_id)
                receipt = await owned_receipt(actor_id, command, lock=True)
                if receipt is None or receipt.result is not None:
                    continue
                receipt.last_attempt_at = utcnow()
                await db.commit()
                await apply_saved(actor_id, command)
        except Exception:
            logger.exception("Original ordinary cancellation recovery remains pending")
