"""One original contract's actual declaration, receipt, processing and evidence.

A source account erasure is never a cancellation declaration. Accepted declarations
remain actionable after target erasure, and receipt replay never selects a new booking.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select as sql_select

from api.database import db, filter_by, select
from api.models import BookingPayment, EventRightGrant, RetainedEventRight, Slot, Webinar, WebinarParticipant
from api.models.event_cancellation import EventCancellation, EventCancellationClaimEvidence
from api.services import booking_contracts, payment_claims, retained_events, settlements, shop
from api.utils.utc import utcnow


def received_instant(receipt: EventCancellation) -> datetime:
    # The validated immutable authority keeps its full precision. Some native
    # DATETIME columns round subsecond metadata; that cannot move a legal
    # qualification boundary or replace the original receipt instant.
    return datetime.fromisoformat(receipt.original["received_at"].replace("Z", "+00:00")).astimezone(timezone.utc)


def result(receipt: EventCancellation, state: str, **details: Any) -> dict[str, Any]:
    return {
        "command_id": receipt.id,
        "source_subject": receipt.source_subject,
        "right_id": receipt.right_id,
        "state": state,
        "received_at": received_instant(receipt).isoformat(),
        "financial_satisfaction": False,
        **details,
    }


async def receive(source: str, command: str) -> dict[str, Any]:
    command = str(UUID(command))
    saved = await db.get(EventCancellation, id=command)
    if saved is not None:
        if saved.source_subject != source:
            raise HTTPException(409, "Different original cancellation owner")
        if saved.result is not None:
            return saved.result
    else:
        receipt = await shop.commercial(
            "event_cancellation_authority", {"source_subject": source, "command_id": command}
        )
        try:
            assert isinstance(receipt, dict)
            declaration = receipt["declaration"]
            received = datetime.fromisoformat(receipt["received_at"].replace("Z", "+00:00"))
            right_id = str(UUID(str(receipt["right_id"]).strip()))
            assert received.tzinfo is not None and receipt["command_id"] == command
            assert receipt["protocol"] == 1 and receipt["source_subject"] == source
            assert receipt["purpose"] == "cancel_identified_event_contract"
            assert receipt["source"] == "authenticated_claimant_declaration"
            assert declaration["cancel_identified_contract"] is True
            assert str(UUID(str(declaration["right_id"]).strip())) == right_id
            assert str(UUID(str(declaration["source_subject"]).strip())) == source
            assert isinstance(declaration["original_text"], str) and declaration["original_text"].strip()
            received = received.astimezone(timezone.utc)
        except (AssertionError, KeyError, ValueError, TypeError, AttributeError):
            raise HTTPException(503, "Original cancellation declaration unavailable") from None
        await retained_events.lock_subject(source)  # erasure does not prohibit contract cancellation
        keys = await payment_claims.committed_and_local_keys(
            select(EventCancellation.id).where(EventCancellation.id == command)
        )
        saved = (
            await db.first(
                filter_by(EventCancellation, id=command).with_for_update().execution_options(populate_existing=True)
            )
            if keys
            else None
        )
        if saved is not None:
            if saved.source_subject != source or saved.original != receipt:
                raise HTTPException(409, "Conflicting original cancellation declaration")
        else:
            saved = await db.add(
                EventCancellation(
                    id=command,
                    source_subject=source,
                    right_id=right_id,
                    received_at=received,
                    observed_at=utcnow(),
                    original=receipt,
                )
            )
        # Durable original receipt precedes parent/claim processing and survives
        # a later local rollback, remote failure, or target change.
        await db.commit()
    return await process(command)


async def process(command: str) -> dict[str, Any]:
    saved = await db.get(EventCancellation, id=command)
    assert saved is not None
    await retained_events.lock_subject(saved.source_subject)
    saved = await db.first(
        filter_by(EventCancellation, id=command).with_for_update().execution_options(populate_existing=True)
    )
    if saved.result is not None:
        return saved.result
    locators = await payment_claims.committed_and_local_keys(
        sql_select(RetainedEventRight.event_id, RetainedEventRight.payment_id, RetainedEventRight.kind).where(
            RetainedEventRight.id == saved.right_id, RetainedEventRight.source_subject == saved.source_subject
        )
    )
    if not locators:
        saved.result = result(
            saved, "resolution_required", reason="Original right evidence unavailable; declaration retained"
        )
        await db.commit()
        return saved.result
    if len(locators) != 1:
        raise HTTPException(503, "Original right locator changed; declaration retained")
    event_id, payment_id, kind = next(iter(locators))
    # The surviving original payment serializes both contractual roles even
    # after ended-event cleanup removed the parent. Never hold one role's mutable
    # right while waiting for that payment and later request the opposite right.
    model = Webinar if kind == "webinar" else Slot
    event = await db.first(filter_by(model, id=event_id).with_for_update().execution_options(populate_existing=True))
    payment = await db.first(
        filter_by(BookingPayment, id=payment_id).with_for_update().execution_options(populate_existing=True)
    )
    right = await db.first(
        filter_by(RetainedEventRight, id=saved.right_id, source_subject=saved.source_subject)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        payment is None
        or right is None
        or payment.id != right.payment_id
        or payment.event_id != right.event_id
        or payment.kind != right.kind
        or (right.event_id, right.payment_id, right.kind) != (event_id, payment_id, kind)
    ):
        raise HTTPException(503, "Original booking payment requires reconciliation; declaration retained")
    start = datetime.fromisoformat(right.original["start"].replace("Z", "+00:00"))
    received = received_instant(saved)
    batch = await settlements.new_batch("cancellation", saved.source_subject, right.event_id)
    # The original declared chronology supplies qualification, not this later
    # processing clock or an account's first unrelated T0/T2 receipt.
    batch.created_at = received
    basis = {
        "request_kind": "identified_service_cancellation",
        "receipt_source": "authenticated_claimant_declaration",
        "cancellation_command_id": saved.id,
        "request_received_at": received.isoformat(),
        "contract_id": payment.id,
        "original_right_id": right.id,
        "role": right.role,
        "cancellation_inferred_from_erasure": False,
        "scope": "one_original_booking",
    }
    facts = payment.original.get("commercial_event", {})
    instructor = facts.get("instructor_id")
    if not instructor:
        contract = await db.get(booking_contracts.BookingContract, id=payment.id)
        instructor = contract.offer.get("product", {}).get("facts", {}).get("instructor_id") if contract else None
    if right.role == "instructor":
        await payment_claims.credit(
            batch.id,
            right.event_id,
            payment.user_id,
            [payment],
            "Identified provider cancellation",
            False,
            entitlement="established" if received <= start else "pending_evidence",
            basis=basis
            | {
                "event_start": start.isoformat(),
                "cancellation_by": "provider",
                "assessment": "service_not_provided" if received <= start else "actual_performance_review",
            },
        )
        if received > start and instructor:
            await payment_claims.credit(
                batch.id,
                right.event_id,
                instructor,
                [payment],
                "Accrued event remuneration",
                True,
                field="payout_coins",
                entitlement="pending_evidence",
                basis=basis | {"assessment": "actual_performance_and_remuneration_review"},
            )
    elif right.role == "participant" and instructor:
        await payment_claims.cancel_student(
            batch.id, right.event_id, payment.user_id, instructor, payment, start, received, basis
        )
    else:
        await payment_claims.credit(
            batch.id,
            right.event_id,
            payment.user_id,
            [payment],
            "Identified cancellation requires original evidence",
            False,
            entitlement="pending_evidence",
            basis=basis | {"assessment": "original_provider_evidence_required"},
        )
    # Exact payment identity prevents old retries/cancellations from touching a
    # replacement booking at the same slot. A host's one-right instruction does
    # not become a declaration to cancel the entire webinar.
    occupied = False
    if event is not None and right.kind == "webinar":
        booked = await db.first(
            filter_by(WebinarParticipant, webinar_id=right.event_id, payment_id=payment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if booked is not None:
            await db.delete(booked)
            occupied = True
    elif event is not None and event.payment_id == payment.id:
        event.cancel()
        occupied = True
    await booking_contracts.close(payment)
    related = await db.all(
        filter_by(RetainedEventRight, payment_id=payment.id).with_for_update().execution_options(populate_existing=True)
    )
    for original in related:
        original.state = "cancelled"
        original.current_subject = None
        for grant in await db.all(
            filter_by(EventRightGrant, right_id=original.id, state="granted")
            .with_for_update()
            .execution_options(populate_existing=True)
        ):
            grant.state = "withdrawn"
    batch_id = batch.id
    committed_result = result(
        saved,
        "applied",
        payment_id=payment.id,
        event_id=right.event_id,
        occupied_booking_removed=occupied,
        settlement_batch_id=batch_id,
        financial_status="claim_recorded",
        unrelated_bookings_cancelled=False,
    )
    saved.result = committed_result
    await db.commit()
    # Existing settlement handoff remains separately retryable; its ambiguity
    # cannot replace the original cancellation receipt or imply payment.
    try:
        await settlements.finish([batch_id])
    except Exception:
        await db.session.rollback()
    # Rollback expires ORM attributes even after the cancellation committed.
    # Return the original plain result without implicit async reloads.
    return committed_result


async def observe_claim(
    claim: Any, command: str, payments: list[BookingPayment], entitlement: str, basis: dict[str, Any]
) -> None:
    receipt = await db.get(EventCancellation, id=command)
    if receipt is None:
        raise ValueError("Cancellation claim evidence requires its durable original declaration")
    assessment = {
        "payment_ids": sorted(payment.id for payment in payments),
        "entitlement": entitlement,
        "basis": basis,
        "original_receipt_time": received_instant(receipt).isoformat(),
    }
    existing = next(
        (
            row
            for row in await claim_evidence(claim.id, current=True)
            if row["origin"] == "retained_claimant" and row["command_id"] == command
        ),
        None,
    )
    if existing is not None:
        if existing["assessment"] != assessment:
            raise ValueError("Conflicting original cancellation assessment")
        return
    await db.add(
        EventCancellationClaimEvidence(
            command_id=command, claim_id=claim.id, observed_at=utcnow(), assessment=assessment
        )
    )
    await qualify_claim(claim)


async def qualify_claim(claim: Any) -> None:
    # Original claim basis/ratio/payment identity remain intact. Only a fully
    # covered, same-basis unpaid entitlement gains the supported qualification.
    # Partial legacy aggregates or different historical amounts require explicit
    # component review; the new evidence is retained and exported independently.
    observations = await claim_evidence(claim.id, current=True)
    qualified = set()
    for observation in observations:
        assessment = observation["assessment"]
        for fact in assessment.get("components", [assessment]):
            if (
                fact["entitlement"] == "established"
                and fact["basis"].get("amount_field") == claim.amount_field
                and fact["basis"].get("ratio") == claim.ratio
            ):
                qualified.update(fact["payment_ids"])
    if set(claim.payment_ids) <= qualified and claim.entitlement == "pending_evidence":
        claim.entitlement = "established"


async def claim_evidence(claim_id: str, *, current: bool = False) -> list[dict[str, Any]]:
    query = filter_by(EventCancellationClaimEvidence, claim_id=claim_id).order_by(
        EventCancellationClaimEvidence.observed_at, EventCancellationClaimEvidence.command_id
    )
    # Qualification owns the parent/claim locks and must include evidence that
    # committed while waiting, regardless of an earlier RR snapshot. Read-only
    # exports do not acquire these mutation locks.
    if current:
        keys = await payment_claims.committed_and_local_keys(
            sql_select(EventCancellationClaimEvidence.command_id, EventCancellationClaimEvidence.claim_id).where(
                EventCancellationClaimEvidence.claim_id == claim_id
            )
        )
        rows = []
        for command, owner_claim in sorted(keys):
            row = await db.first(
                filter_by(EventCancellationClaimEvidence, command_id=command, claim_id=owner_claim)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None:
                raise ValueError("Original claim evidence changed during owning reconciliation")
            rows.append(row)
    else:
        rows = await db.all(query)
    from api.services import ordinary_cancellations

    observations = [
        {
            "origin": "retained_claimant",
            "command_id": row.command_id,
            "observed_at": row.observed_at.isoformat(),
            "assessment": row.assessment,
        }
        for row in rows
    ]
    observations.extend(await ordinary_cancellations.claim_evidence(claim_id, current=current))
    return sorted(observations, key=lambda item: (item["observed_at"], item["origin"], item["command_id"]))


async def export(subject: str) -> list[dict[str, Any]]:
    rows = await db.all(
        filter_by(EventCancellation, source_subject=subject).order_by(
            EventCancellation.received_at, EventCancellation.id
        )
    )
    return [
        {column.name: getattr(row, column.name) for column in row.__table__.columns}
        | {"received_at": received_instant(row)}
        for row in rows
    ]


async def recover() -> None:
    """Fresh owning session per accepted receipt; no nested pool acquisition.

    Backend's immutable receipt also covers a request whose first Events call
    never arrived. Exact local result replay survives a lost outcome response.
    """
    from api.database import db_context
    from api.logger import get_logger

    pending = await shop.commercial("event_cancellation_pending", {})
    if not isinstance(pending, list):
        raise ValueError("Cancellation recovery inventory unavailable")
    for row in pending[:10]:
        try:
            source, command = str(UUID(row["source_subject"])), str(UUID(row["command_id"]))
            # Record an attempt before a potentially unavailable service call.
            # A unique observation rotates uncertain work behind older attempts,
            # without changing the accepted declaration or claiming any effect.
            await shop.commercial(
                "event_cancellation_outcome",
                {
                    "command_id": command,
                    "outcome": {
                        "command_id": command,
                        "source_subject": source,
                        "right_id": str(UUID(str(row["right_id"]).strip())),
                        "state": "uncertain",
                        "financial_satisfaction": False,
                        "attempt_id": str(uuid4()),
                        "reason": "Service processing attempt started; outcome not yet observed",
                    },
                },
            )
            async with db_context():
                outcome = await receive(source, command)
            await shop.commercial("event_cancellation_outcome", {"command_id": command, "outcome": outcome})
        except Exception:
            get_logger(__name__).exception("Original cancellation remains pending for exact recovery")
