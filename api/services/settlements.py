"""Commit financial obligations together with event changes, then deliver immutable credits."""

from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from api.database import db, db_wrapper, filter_by, select
from api.logger import get_logger
from api.models.booking_payment import SettlementClaim
from api.models.settlement import CoinOperation, SettlementBatch
from api.services import commercial, payment_claims, shop
from api.utils import email
from api.utils.utc import utcnow


logger = get_logger(__name__)


async def new_batch(kind: str, actor_id: str | None = None, event_id: str | None = None) -> SettlementBatch:
    return await db.add(
        SettlementBatch(
            id=str(uuid4()), kind=kind, actor_id=actor_id, event_id=event_id, notifications=[], created_at=utcnow()
        )
    )


async def credit(
    batch: SettlementBatch, event_id: str, user_id: str, coins: int, description: str, credit_note: bool
) -> None:
    if coins:
        await db.add(
            CoinOperation(
                batch_id=batch.id,
                event_id=event_id,
                user_id=user_id,
                coins=coins,
                description=description,
                credit_note=credit_note,
            )
        )


def notification(batch: SettlementBatch, message: email.Message, user_id: str, **kwargs: Any) -> None:
    batch.notifications = [*batch.notifications, {"template": message.template, "user_id": user_id, "args": kwargs}]


async def finish(batches: list[str]) -> None:
    # This is the durable boundary: no remote effect is allowed before the cancellation
    # and every obligation are committed. Later delivery/ack failures cannot erase them.
    await db.commit()
    pending = await deliver(batches)
    if pending:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "EventSettlementPending",
                "cancellation_committed": True,
                "pending_operations": pending,
                "reconciliation_may_be_required": True,
            },
        )


async def deliver(batches: list[str] | None = None) -> int:
    await payment_claims.resolve_pending(batches)
    pending_handoffs = await commercial.handoff_claims(batches)
    query = select(CoinOperation).where(CoinOperation.completed_at.is_(None))
    if batches is not None:
        query = query.where(CoinOperation.batch_id.in_(batches))
    # Per-operation commits keep partial progress. Concurrent workers may send the
    # same immutable request; the shop's unique transactional key makes that safe.
    operation_ids = [op.id for op in await db.all(query)]
    for operation_id in operation_ids:
        operation = await db.get(CoinOperation, id=operation_id)
        if operation is None or operation.completed_at is not None or operation.provenance != "ready":
            continue
        # Claim-backed credits use the new disposition protocol. Handoff is not
        # a completed wallet operation and must not trigger a second old PUT.
        if await db.get(SettlementClaim, id=operation.id) is not None:
            continue
        try:
            success = await shop.apply_coin_operation(
                operation.id, operation.user_id, operation.coins, operation.description, bool(operation.credit_note)
            )
        except Exception as exc:
            operation.last_error = type(exc).__name__[:80]
        else:
            if success:
                operation.completed_at = utcnow()
                operation.last_error = None
            else:
                operation.last_error = "ShopRejectedOperation"
        operation.attempts += 1
        await db.commit()
    batch_query = select(SettlementBatch).where(SettlementBatch.notified_at.is_(None))
    if batches is not None:
        batch_query = batch_query.where(SettlementBatch.id.in_(batches))
    for batch in await db.all(batch_query):
        # These stamps describe actual SMTP acceptance of the new truthful
        # notice. Older notified_at assertions are preserved as historical facts.
        items = list(batch.notifications)
        for i, item in enumerate(items):
            if item.get("commercial_smtp_accepted_at"):
                continue
            notice = item.get("args", {}).get("ordinary_cancellation")
            accepted = (
                await email.notify_commercial(item["user_id"], batch.id, notice=notice)
                if notice is not None
                else await email.notify_commercial(item["user_id"], batch.id)
            )
            if accepted:
                items[i] = item | {"commercial_smtp_accepted_at": utcnow().isoformat()}
                batch.notifications = list(items)
                await db.commit()
        if all(item.get("commercial_smtp_accepted_at") for item in items):
            batch.notified_at = utcnow()
            await db.commit()
    # A safely handed-off unknown amount is still a financial review, but does
    # not turn completed erasure into endless failed delivery or a fake credit.
    unresolved_operations = query.where(~CoinOperation.id.in_(select(SettlementClaim.id)))
    return pending_handoffs + await db.count(unresolved_operations)


@db_wrapper
async def recover_settlements() -> None:
    pending = await deliver()
    if pending:
        logger.warning("Event coin operations remain pending: %s", pending)
