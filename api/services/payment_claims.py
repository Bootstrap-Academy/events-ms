"""Unknown amounts remain dated claims, separate from executable coin credits."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from api.database import db, filter_by, select
from api.models.booking_payment import BookingPayment, CommercialBatchClaim, SettlementClaim
from api.models.settlement import CoinOperation
from api.services.booking_payments import share
from api.utils.utc import utcnow


async def committed_and_local_keys(query: Any) -> set[Any]:
    """Discover existing keys without locking an absent InnoDB index range.

    Owning parent/claim serialization protects the business identity. Include
    this transaction's flushed additions and a fresh committed view after any
    lock wait. Subsequent mutation reads lock only the confirmed existing keys.
    The reserved reader is nonlocking and never enters the owning pool.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession

    keys = set((await db.exec(query)).all())
    if db.engine.dialect.name == "sqlite":
        return keys
    if db.committed_read_engine is None:
        raise RuntimeError("Committed claim discovery reader was not reserved at startup")
    async with (
        asyncio.timeout(5),
        AsyncSession(db.committed_read_engine, autoflush=False, expire_on_commit=False) as session,
    ):
        keys.update((await session.execute(query)).all())
    return keys


async def existing_claims(event_id: str, user_id: str) -> list[SettlementClaim]:
    keys = await committed_and_local_keys(
        select(SettlementClaim.id).where(SettlementClaim.event_id == event_id, SettlementClaim.user_id == user_id)
    )
    claims = []
    for (claim_id,) in sorted(keys):
        claim = await db.first(
            filter_by(SettlementClaim, id=claim_id).with_for_update().execution_options(populate_existing=True)
        )
        if claim is None or claim.event_id != event_id or claim.user_id != user_id:
            raise ValueError("Original claim identity changed during owning reconciliation")
        claims.append(claim)
    return claims


def amount(payments: list[BookingPayment], field: str, ratio: str) -> int | None:
    if Decimal(ratio) == 0:
        return 0
    values = [getattr(payment, field) for payment in payments]
    return None if any(value is None for value in values) else share(sum(values), ratio)


async def credit(
    batch_id: str,
    event_id: str,
    user_id: str,
    payments: list[BookingPayment],
    description: str,
    credit_note: bool,
    ratio: str = "1",
    field: str = "paid_coins",
    entitlement: str = "established",
    basis: dict[str, Any] | None = None,
    observations: list[tuple[Any, list[BookingPayment], str, dict[str, Any]]] | None = None,
) -> int | dict[str, str]:
    if not payments:
        return 0
    # Event/slot writers serialize before this call. A legacy aggregate claim
    # can cover only part of a later request: link its original identity and create
    # obligations solely for previously uncovered payments, never a second credit.
    payment_ids = sorted(payment.id for payment in payments)
    covered: set[str] = set()
    reused = []
    for existing in await existing_claims(event_id, user_id):
        overlap = set(existing.payment_ids) & set(payment_ids)
        if overlap:
            covered.update(overlap)
            reused.append(existing)
            if basis and (basis.get("cancellation_command_id") or basis.get("ordinary_cancellation_command_id")):
                await observe_cancellation(
                    existing,
                    [payment for payment in payments if payment.id in overlap],
                    entitlement,
                    basis | {"amount_field": field, "ratio": ratio},
                    observations,
                )
            if not await db.exists(filter_by(CommercialBatchClaim, batch_id=batch_id, claim_id=existing.id)):
                await db.add(CommercialBatchClaim(batch_id=batch_id, claim_id=existing.id))
    payments = [payment for payment in payments if payment.id not in covered]
    if not payments:
        return {"payment_claim": reused[0].id}
    # Single-payment components remain independently reconcilable even when a
    # later account deletion or cleanup collects a different group of bookings.
    if len(payments) > 1:
        refs = [
            await credit(
                batch_id,
                event_id,
                user_id,
                [payment],
                description,
                credit_note,
                ratio,
                field,
                entitlement,
                basis,
                observations,
            )
            for payment in payments
        ]
        return (
            sum(value for value in refs if isinstance(value, int))
            if all(isinstance(value, int) for value in refs)
            else {"payment_claim": "multiple"}
        )
    payment_ids = [payments[0].id]
    claim = await db.add(
        SettlementClaim(
            id=str(uuid4()),
            batch_id=batch_id,
            event_id=event_id,
            user_id=user_id,
            payment_ids=payment_ids,
            amount_field=field,
            ratio=ratio,
            description=description,
            credit_note=credit_note,
            entitlement=entitlement,
            basis=basis or {},
        )
    )
    # These models have no ORM relationship; flush the owning claim before its
    # link, while retaining the single cancellation/erasure transaction.
    await db.session.flush()
    await db.add(CommercialBatchClaim(batch_id=batch_id, claim_id=claim.id))
    if basis and (basis.get("cancellation_command_id") or basis.get("ordinary_cancellation_command_id")):
        await observe_cancellation(
            claim, payments, entitlement, basis | {"amount_field": field, "ratio": ratio}, observations
        )
    await resolve(claim, payments)
    return claim.coins if claim.coins is not None else {"payment_claim": claim.id}


async def observe_cancellation(
    claim: SettlementClaim,
    payments: list[BookingPayment],
    entitlement: str,
    basis: dict[str, Any],
    observations: list[tuple[Any, list[BookingPayment], str, dict[str, Any]]] | None = None,
) -> None:
    from api.services import event_cancellations, ordinary_cancellations

    retained = basis.get("cancellation_command_id")
    ordinary = basis.get("ordinary_cancellation_command_id")
    if bool(retained) == bool(ordinary):
        raise ValueError("Cancellation assessment must identify exactly one receipt origin")
    if observations is not None:
        if not ordinary:
            raise ValueError("Only a complete ordinary command collects component assessments")
        observations.append((claim, payments, entitlement, basis))
    elif ordinary:
        await ordinary_cancellations.observe_claim(claim, ordinary, payments, entitlement, basis)
    else:
        await event_cancellations.observe_claim(claim, retained, payments, entitlement, basis)


async def resolve(claim: SettlementClaim, payments: list[BookingPayment]) -> None:
    coins = amount(payments, claim.amount_field, claim.ratio)
    if coins is None:
        return
    claim.coins = coins
    claim.resolved_at = utcnow()
    if coins:
        await db.add(
            CoinOperation(
                id=claim.id,
                batch_id=claim.batch_id,
                event_id=claim.event_id,
                user_id=claim.user_id,
                coins=coins,
                description=claim.description,
                credit_note=claim.credit_note,
            )
        )


async def resolve_pending(batches: list[str] | None) -> None:
    query = filter_by(SettlementClaim, resolved_at=None)
    if batches is not None:
        query = query.where(SettlementClaim.batch_id.in_(batches))
    ids = [claim.id for claim in await db.all(query)]
    for claim_id in ids:
        claim = await db.first(
            filter_by(SettlementClaim, id=claim_id).with_for_update().execution_options(populate_existing=True)
        )
        if claim is None or claim.resolved_at is not None:
            continue
        payments = await db.all(select(BookingPayment).where(BookingPayment.id.in_(claim.payment_ids)))
        if len(payments) != len(claim.payment_ids):
            raise ValueError("Payment evidence missing for settlement claim")
        await resolve(claim, payments)
    await db.commit()


async def notification_args(args: dict[str, Any]) -> dict[str, Any]:
    coins = args.get("coins")
    if isinstance(coins, dict) and "payment_claim" in coins:
        claim = await db.get(SettlementClaim, id=coins["payment_claim"])
        if claim is None or claim.coins is None:
            raise ValueError("Unresolved payment claim cannot be described as refunded")
        return args | {"coins": claim.coins}
    return args


async def cancel_student(
    batch_id: str,
    event_id: str,
    student: str,
    instructor: str,
    payment: BookingPayment,
    start: datetime,
    received: datetime | None,
    basis: dict[str, Any],
    observations: list[tuple[Any, list[BookingPayment], str, dict[str, Any]]] | None = None,
) -> tuple[int | dict[str, str], int | dict[str, str]]:
    """Preserve the existing seven-day return; assess other lawful claims individually.

    There is no automated half-price/total-forfeiture substitute. The original
    actual debit and agreed instructor amount are evidence ceilings, not an
    assertion that either contingent claim has already been established.
    """
    full = received is not None and start - received >= timedelta(days=7)
    facts = basis | {
        "request_received_at": received.isoformat() if received else None,
        "event_start": start.isoformat(),
        "cancellation_by": "student",
        "assessment": "existing_seven_day_return" if full else "individual_entitlement_and_loss_review",
    }
    student_claim = await credit(
        batch_id,
        event_id,
        student,
        [payment],
        "Event cancellation",
        False,
        entitlement="established" if full else "pending_evidence",
        basis=facts,
        observations=observations,
    )
    instructor_claim = (
        0
        if full
        else await credit(
            batch_id,
            event_id,
            instructor,
            [payment],
            "Event cancellation remuneration claim",
            True,
            field="payout_coins",
            entitlement="pending_evidence",
            basis=facts,
            observations=observations,
        )
    )
    return student_claim, instructor_claim
