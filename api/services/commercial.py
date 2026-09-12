"""Original event claims are handed to one backend disposition authority.

The local erasure/claim commit, the handoff acknowledgment and actual financial
satisfaction are intentionally distinct. No shop balance is fabricated.
"""

import hashlib
import json
from datetime import datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from api.database import db, filter_by, select
from api.models.booking_payment import (
    BookingPayment,
    CommercialBatchClaim,
    CommercialErasureReceipt,
    CommercialHandoff,
    SettlementClaim,
)
from api.models.settlement import CoinOperation
from api.services import shop
from api.utils.utc import utcnow


async def erasure_receipt(subject: str) -> CommercialErasureReceipt:
    # Remote observation comes before local erasure/parent locks. Absence/outage
    # is unknown evidence, not permission to invent an original receipt time.
    try:
        canonical = await shop.commercial("erasure", {"subject": subject})
    except Exception:
        canonical = None
    if canonical is not None and (
        canonical.get("protocol") != 1 or canonical.get("subject") != subject or not canonical.get("case_id")
    ):
        raise ValueError("Mismatched canonical erasure receipt")
    receipt = await db.get(CommercialErasureReceipt, subject=subject)
    if receipt is None:
        try:
            async with db.session.begin_nested():
                await db.add(CommercialErasureReceipt(subject=subject, observed_at=utcnow()))
                await db.session.flush()
        except IntegrityError:
            pass  # another erasure already inserted the same stable receipt
    receipt = await db.first(
        filter_by(CommercialErasureReceipt, subject=subject).with_for_update().execution_options(populate_existing=True)
    )
    if receipt is None:
        raise ValueError("Erasure receipt was not preserved")
    if receipt.canonical is None:
        receipt.canonical = canonical
    elif canonical is not None and receipt.canonical != canonical:
        raise ValueError("Original erasure receipt changed")
    return cast(CommercialErasureReceipt, receipt)


async def payment_for_erasure(booking: Any, kind: str, event_id: str, student: str) -> BookingPayment:
    payment: BookingPayment | None = (
        cast(
            BookingPayment | None,
            await db.first(
                filter_by(BookingPayment, id=booking.payment_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )
        if booking.payment_id
        else None
    )
    if payment is not None:
        return payment
    # Evidence of an uninstrumented booking is a new observation, not a made-up
    # historical debit/operation join or a zero payment. Keep only financial facts.
    payment = await db.add(
        BookingPayment(
            id=str(uuid4()),
            event_id=event_id,
            user_id=student,
            kind=kind,
            state="legacy_unknown",
            quoted_coins=None,
            paid_coins=None,
            payout_coins=None,
            payout_ratio=None,
            description="Unresolved booking observed before account erasure",
            original={
                "kind": "erasure_booking_observation",
                "observed_at": utcnow().isoformat(),
                "previous_payment_reference": booking.payment_id,
                "asserted_student_coins": getattr(booking, "paid_coins", getattr(booking, "student_coins", None)),
                "asserted_instructor_coins": getattr(booking, "instructor_coins", None),
            },
            evidence=None,
        )
    )
    booking.payment_id = payment.id
    return payment


async def acknowledge_erasure(subject: str) -> None:
    receipt = await erasure_receipt(subject)
    if receipt.erased_at is None or receipt.canonical is None:
        await db.commit()
        from fastapi import HTTPException

        raise HTTPException(
            503, detail={"code": "CommercialInventoryPending", "erasure_committed": receipt.erased_at is not None}
        )
    payload = {
        "subject": subject,
        "service": "events",
        "state": "erased",
        "evidence": {
            "canonical_request_id": receipt.canonical["request"]["id"],
            "local_erased_at": receipt.erased_at.isoformat(),
        },
    }
    payload["command_id"] = str(uuid5(NAMESPACE_URL, "bootstrap-events-erasure:" + json.dumps(payload, sort_keys=True)))
    await db.commit()
    result = await shop.commercial("inventory", payload)
    if result is None or result.get("accepted") is not True or result.get("financial_satisfaction") is not False:
        raise ValueError("Inventory acknowledgment is not financial settlement")
    ack_receipt = await db.first(
        filter_by(CommercialErasureReceipt, subject=subject).with_for_update().execution_options(populate_existing=True)
    )
    if ack_receipt is not None:
        ack_receipt.acknowledged_at = ack_receipt.acknowledged_at or utcnow()
    await db.commit()


def received_at(receipt: CommercialErasureReceipt) -> datetime | None:
    if receipt.canonical is None:
        return None
    original = receipt.canonical.get("request", {})
    if original.get("source") not in {"authenticated_service_receipt", "verified_earlier_declaration"}:
        return None
    value = original.get("received_at")
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None


async def handoff(claim: SettlementClaim) -> bool:
    from api.services import event_cancellations, payment_claims

    current = await db.first(
        filter_by(SettlementClaim, id=claim.id).with_for_update().execution_options(populate_existing=True)
    )
    if current is None:
        raise ValueError("Original claim disappeared")
    claim = cast(SettlementClaim, current)
    if claim is None:
        raise ValueError("Original claim disappeared")
    payments = await db.all(select(BookingPayment).where(BookingPayment.id.in_(claim.payment_ids)))
    if len(payments) != len(claim.payment_ids):
        raise ValueError("Missing original booking evidence")
    # The owning claim lock does not refresh an earlier RR snapshot. Discover
    # committed/own children without an absent-key range lock, then refresh only
    # confirmed exact rows. Payment locks here would invert Payment -> Claim.
    operation_keys = await payment_claims.committed_and_local_keys(
        select(CoinOperation.id).where(CoinOperation.id == claim.id)
    )
    operation = (
        await db.first(
            filter_by(CoinOperation, id=claim.id).with_for_update().execution_options(populate_existing=True)
        )
        if operation_keys
        else None
    )
    if operation_keys and operation is None:
        raise ValueError("Original claim operation disappeared")
    student = bool(payments) and all(p.user_id == claim.user_id for p in payments)
    identity = {
        "source_key": claim.id,
        "component": "student_refund" if student else "instructor_remuneration",
        "event_id": claim.event_id,
        "payment_ids": sorted(claim.payment_ids),
        "amount_field": claim.amount_field,
        "ratio": claim.ratio,
        "description": claim.description,
        "original_claim_created_at": claim.created_at.isoformat(),
    }
    observation = {
        "units": claim.coins if claim.entitlement == "established" else None,
        "computed_units": claim.coins,
        "entitlement": claim.entitlement,
        "basis": claim.basis,
        "cancellation_evidence": await event_cancellations.claim_evidence(claim.id, current=True),
        "payments": [
            {
                "id": p.id,
                "state": p.state,
                "paid_coins": p.paid_coins,
                "payout_coins": p.payout_coins,
                "payout_ratio": p.payout_ratio,
                "actual_ledger_id": p.ledger_transaction_id,
                "source_evidence_sha256": hashlib.sha256(
                    json.dumps({"evidence": p.evidence, "original": p.original}, sort_keys=True, default=str).encode()
                ).hexdigest(),
                "original_contract_id": p.id if (p.evidence or {}).get("kind") == "purchase_contract" else None,
            }
            for p in sorted(payments, key=lambda p: p.id)
        ],
    }
    payload = {"subject": claim.user_id, "obligation_id": claim.id, "identity": identity, "observation": observation}
    if operation is not None:
        payload |= {
            "operation_id": operation.id,
            "operation_payload": {
                "coins": operation.coins,
                "description": operation.description,
                "credit_note": bool(operation.credit_note),
            },
        }
    payload["command_id"] = str(
        uuid5(NAMESPACE_URL, "bootstrap-commercial-event:" + json.dumps(payload, sort_keys=True, separators=(",", ":")))
    )
    handoff_keys = await payment_claims.committed_and_local_keys(
        select(CommercialHandoff.claim_id).where(CommercialHandoff.claim_id == claim.id)
    )
    saved = (
        await db.first(
            filter_by(CommercialHandoff, claim_id=claim.id).with_for_update().execution_options(populate_existing=True)
        )
        if handoff_keys
        else None
    )
    if handoff_keys and saved is None:
        raise ValueError("Original claim handoff disappeared")
    if saved is not None and saved.payload == payload and saved.acknowledged_at is not None:
        return True
    if saved is None:
        saved = await db.add(CommercialHandoff(claim_id=claim.id, payload=payload))
    else:
        saved.payload = payload
        saved.acknowledged_at = None
        saved.receipt = None
    # Local evidence exists durably before remote handoff, and survives its reply loss.
    await db.commit()
    result = None
    error = None
    try:
        result = await shop.commercial("register_event", payload)
        if result is None or result.get("protocol") != 1 or result.get("obligation_id") != claim.id:
            raise ValueError("Mismatched commercial handoff")
        if result.get("disposition") not in {"claim_preserved", "historical_wallet_application"}:
            raise ValueError("Unknown disposition must not be called paid")
    except Exception as exc:
        error = type(exc).__name__[:80]
    # A late response cannot replace another worker's newer evidence version,
    # and a failed duplicate cannot erase an acknowledgment of this exact version.
    saved = await db.first(
        filter_by(CommercialHandoff, claim_id=claim.id).with_for_update().execution_options(populate_existing=True)
    )
    if saved is None:
        raise ValueError("Durable handoff disappeared")
    if saved.payload == payload:
        saved.attempts += 1
        if error is None:
            saved.receipt = result
            saved.acknowledged_at = saved.acknowledged_at or utcnow()
            saved.last_error = None
        elif saved.acknowledged_at is None:
            saved.last_error = error
    accepted = saved.acknowledged_at is not None
    await db.commit()
    return accepted


async def handoff_claims(batches: list[str] | None) -> int:
    query = select(SettlementClaim)
    if batches is not None:
        linked = select(CommercialBatchClaim.claim_id).where(CommercialBatchClaim.batch_id.in_(batches))
        query = query.where(or_(SettlementClaim.batch_id.in_(batches), SettlementClaim.id.in_(linked)))
    ids = [c.id for c in await db.all(query)]
    pending = 0
    for claim_id in ids:
        claim = await db.get(SettlementClaim, id=claim_id)
        if claim is not None and not await handoff(claim):
            pending += 1
    return pending


async def retain_event(payment: BookingPayment, event: Any, instructor: str) -> dict[str, Any]:
    """Retain necessary booking topology before a cleanup can remove its parents.

    A retained L1 offer supplies the original agreed facts. An older current row
    is labelled as an observation; it is not promoted to historic assent/payment.
    """
    from api.models.booking_contract import BookingContract

    original = payment.original.get("commercial_event")
    if isinstance(original, dict):
        return original
    contract = await db.get(BookingContract, id=payment.id)
    offer_facts = contract.offer.get("product", {}).get("facts", {}) if contract is not None else {}
    facts = {
        "instructor_id": instructor,
        "start": offer_facts.get("start", event.start.isoformat()),
        "end": offer_facts.get("end", event.end.isoformat()),
        "source": "original_offer" if offer_facts else "event_observed_before_removal",
        "observed_at": utcnow().isoformat(),
        "original_contract_id": contract.id if contract is not None else None,
        "performance": "not_proved_by_availability_or_clock",
    }
    payment.original = payment.original | {"commercial_event": facts}
    return facts


async def cleanup_claims(payment: BookingPayment, event: Any, instructor: str, batch_id: str) -> bool:
    """Separate actual cancellation from data erasure and unknown performance.

    Read current scoped recipients while keeping original payment/contract owners
    unchanged. Remote reads take no local receipt lock beneath the event lock.
    """
    from types import SimpleNamespace

    from api.services import payment_claims, retained_events

    facts = await retain_event(payment, event, instructor)
    student_right = await retained_events.right_for(payment.id, "participant")
    provider_right = await retained_events.right_for(payment.id, "instructor")
    student_subject = (
        student_right.current_subject
        if student_right is not None and student_right.state == "active"
        else payment.user_id
    )
    provider_subject = (
        provider_right.current_subject
        if provider_right is not None and provider_right.state == "active"
        else instructor
    )
    student = provider = None
    uncertain = False
    try:
        student = await shop.commercial("erasure", {"subject": student_subject})
        provider = await shop.commercial("erasure", {"subject": provider_subject})
    except Exception:
        uncertain = True
    for source, subject in ((student, student_subject), (provider, provider_subject)):
        if source is not None and (source.get("protocol") != 1 or source.get("subject") != subject):
            raise ValueError("Mismatched erasure receipt")
    unresolved_right = any(
        r is not None and r.state in {"preserved", "resolution_pending"} for r in (student_right, provider_right)
    )
    if student is None and provider is None and not uncertain and not unresolved_right:
        return False
    observed = utcnow()
    for source, subject, role in (
        (student, student_subject, "participant"),
        (provider, provider_subject, "instructor"),
    ):
        if source is not None:
            await retained_events.preserve_on_erasure(
                cast(str, subject), SimpleNamespace(canonical=source, observed_at=observed), payment, event, role
            )
    # Ended availability is not proof of attendance/performance. These rights
    # need actual resolution after their original period, never invented renewal.
    for role in ("participant", "instructor"):
        right = await retained_events.right_for(payment.id, role)
        if right is not None and right.state == "preserved":
            right.state = "resolution_pending"
    student_cancel = retained_events.cancellation_declaration(student, payment.id)
    provider_cancel = retained_events.cancellation_declaration(provider, payment.id)
    declaration = provider_cancel or student_cancel
    received = retained_events.declaration_time(declaration) if declaration else None
    start = datetime.fromisoformat(facts["start"].replace("Z", "+00:00"))
    original_instructor = facts.get("instructor_id", instructor)
    basis = {
        "request_id": batch_id,
        "request_kind": "identified_service_cancellation" if declaration else "performance_resolution",
        "cancellation_declaration": (
            retained_events.declaration_basis(declaration, payment.id) if declaration else None
        ),
        "request_received_at": received.isoformat() if received else None,
        "erasure_observed": {"participant": student is not None, "instructor": provider is not None},
        "event_start": facts["start"],
        "event_end": facts["end"],
        "financial_source": facts["source"],
        "assessment": "actual_declaration_or_performance_review",
        "cancellation_inferred_from_erasure": False,
        "backend_temporarily_unavailable": uncertain,
    }
    if student_cancel is not None and provider_cancel is None:
        await payment_claims.cancel_student(
            batch_id, payment.event_id, payment.user_id, original_instructor, payment, start, received, basis
        )
    else:
        await payment_claims.credit(
            batch_id,
            payment.event_id,
            payment.user_id,
            [payment],
            "Identified provider cancellation" if provider_cancel else "Event performance requires review",
            False,
            entitlement=(
                "established" if provider_cancel and received is not None and received <= start else "pending_evidence"
            ),
            basis=basis | {"cancellation_by": "provider" if provider_cancel else "not_established"},
        )
        await payment_claims.credit(
            batch_id,
            payment.event_id,
            original_instructor,
            [payment],
            "Event remuneration requires review",
            True,
            field="payout_coins",
            entitlement="pending_evidence",
            basis=basis,
        )
    return True


async def preserve_detached_claims(subject: str, receipt: CommercialErasureReceipt, batch_id: str) -> None:
    """Financial source records also outlive cleanup; parent absence is not no claim."""
    from api.models.booking_contract import BookingContract
    from api.services import payment_claims, retained_events

    instructor_contracts = select(BookingContract.id).where(
        BookingContract.offer["product"]["facts"]["instructor_id"].as_string() == subject
    )
    payment_keys = await payment_claims.committed_and_local_keys(
        select(BookingPayment.id).where(
            or_(
                BookingPayment.user_id == subject,
                BookingPayment.original["commercial_event"]["instructor_id"].as_string() == subject,
                BookingPayment.id.in_(instructor_contracts),
            )
        )
    )
    payments = []
    for (payment_id,) in sorted(payment_keys):
        # Original payments survive their event. Cancellation owns this same
        # durable row before rights/grants/claims, including after cleanup.
        payment = await db.first(
            filter_by(BookingPayment, id=payment_id).with_for_update().execution_options(populate_existing=True)
        )
        if payment is None:
            raise ValueError("Original detached payment disappeared")
        payments.append(payment)
    # Acquire the full ordered inventory before shared/aggregate claim rows or
    # mutable rights. A claim can cover more than one surviving payment.
    requested = received_at(receipt)
    for payment in payments:
        if await retained_events.preserved(payment.id, subject):
            continue
        declaration = retained_events.cancellation_declaration(receipt.canonical, payment.id)
        cancelled_at = retained_events.declaration_time(declaration) if declaration else None
        facts = payment.original.get("commercial_event")
        if not isinstance(facts, dict):
            contract = await db.get(BookingContract, id=payment.id)
            facts = contract.offer.get("product", {}).get("facts", {}) if contract is not None else {}
        instructor = facts.get("instructor_id")
        start_value = facts.get("start")
        start = datetime.fromisoformat(start_value.replace("Z", "+00:00")) if isinstance(start_value, str) else None
        basis = {
            "request_id": batch_id,
            "request_kind": "account_erasure",
            "canonical_request_id": ((receipt.canonical or {}).get("request") or {}).get("id"),
            "request_received_at": requested.isoformat() if requested else None,
            "receipt_source": "canonical_backend" if requested else "original_receipt_unknown",
            "event_start": start.isoformat() if start else None,
            "assessment": "surviving_booking_evidence_independent_of_parent_cleanup",
            "cancellation_declaration": (
                retained_events.declaration_basis(declaration, payment.id) if declaration else None
            ),
            "cancellation_inferred_from_erasure": False,
        }
        if payment.user_id == subject:
            if declaration is not None and isinstance(instructor, str) and start is not None:
                await payment_claims.cancel_student(
                    batch_id,
                    payment.event_id,
                    subject,
                    instructor,
                    payment,
                    start,
                    cancelled_at,
                    basis
                    | {
                        "request_kind": "identified_service_cancellation",
                        "request_received_at": cast(datetime, cancelled_at).isoformat(),
                    },
                )
            else:
                await payment_claims.credit(
                    batch_id,
                    payment.event_id,
                    subject,
                    [payment],
                    "Erasure with incomplete original event evidence",
                    False,
                    entitlement="pending_evidence",
                    basis=basis,
                )
        if instructor == subject:
            await payment_claims.credit(
                batch_id,
                payment.event_id,
                subject,
                [payment],
                "Surviving event remuneration",
                True,
                field="payout_coins",
                entitlement="pending_evidence",
                basis=basis,
            )
            await payment_claims.credit(
                batch_id,
                payment.event_id,
                payment.user_id,
                [payment],
                "Provider erasure with retained event evidence",
                False,
                entitlement=(
                    "established"
                    if cancelled_at is not None and start is not None and cancelled_at <= start
                    else "pending_evidence"
                ),
                basis=basis | {"cancellation_by": "provider" if declaration else "not_established"},
            )
