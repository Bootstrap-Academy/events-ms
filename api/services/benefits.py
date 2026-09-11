"""Freeze prospective cleanup eligibility with its outbox; retry exact committed XP receipts.

No historical scan: absence of this new earning record does not prove an old
award was unpaid. The original booking/session, never a successor or retry,
identifies an earning. Instructor coins stay in the separate settlement lane.
"""

from datetime import timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import or_

from api.database import db, db_context, filter_by, select
from api.models.benefit import EventBenefit, EventBenefitObservation
from api.services import skills
from api.utils.utc import utcnow


async def record(event: Any, payment: Any, role: str, user_id: str, xp: int) -> str:
    """Called only by the serialized owning cleanup after existing eligibility.

    One instructor award per original session; one participant award per actual
    booking. Old request values are never recomputed from changed configuration.
    The owning parent lock serializes all awards for that session.
    """
    kind = "webinar" if hasattr(event, "creator") else (event.event_type.value if event.event_type else "slot")
    source_id = event.id if role == "instructor" or payment is None else payment.id
    identity = str(uuid5(NAMESPACE_URL, f"bootstrap-events-benefit:v1:{kind}:{source_id}:{role}"))
    prior = await db.first(
        filter_by(EventBenefit, id=identity).with_for_update().execution_options(populate_existing=True)
    )
    if prior is not None:
        return prior.id
    if role == "instructor":
        source_subject = payment.original["commercial_event"]["instructor_id"] if payment else event.user_id
    else:
        source_subject = payment.user_id if payment else event.booked_by
    # New booking provenance does not establish a new hosting session. A
    # legacy host may already have received its unkeyed session award.
    prospective = (
        getattr(event, "xp_delivery_protocol", None) == 1
        if kind == "webinar" and role == "instructor"
        else payment is not None and payment.xp_delivery_protocol == 1
    )
    initial = (
        None
        if prospective
        else {
            "state": "review",
            "reason": "Legacy earning may already have an unkeyed effect; original outcome requires evidence",
            "previous_effect": "unknown",
            "entitlement_forfeited": False,
        }
    )
    now = utcnow()
    earning = str(uuid5(NAMESPACE_URL, f"bootstrap-events-earning:v1:{kind}:{source_id}:{role}"))
    await db.add(
        EventBenefit(
            id=identity,
            source_kind=kind,
            source_id=source_id,
            role=role,
            source_subject=source_subject,
            user_id=user_id,
            event_id=event.id,
            received_at=now,
            original={
                "source": "owning_cleanup_eligibility_observation",
                "issuance_protocol": 1 if prospective else None,
                "new_booking_protocol": payment.xp_delivery_protocol if payment else None,
                "new_session_protocol": getattr(event, "xp_delivery_protocol", None),
                "historic_unkeyed_amount": None,
                "event_id": event.id,
                "payment_id": payment.id if role == "participant" and payment else None,
                "start": event.start.isoformat(),
                "end": event.end.isoformat(),
                "configured_xp": xp,
                "skill_id": event.skill_id,
                "attendance_or_payment_proof_inferred": False,
            },
            request={"user_id": user_id, "skill_id": event.skill_id, "xp": xp, "earning_id": earning},
            state="pending" if prospective else "review",
            receipt=initial,
            attempts=0,
            next_attempt_at=now,
        )
    )
    return identity


async def dispatch_one() -> bool:
    """Own a fresh transaction, so uncommitted producer rows cannot dispatch.

    Keep the row lock through receipt commit. Reply loss or local commit failure
    retries the same operation; a later failed worker cannot overwrite success.
    """
    async with db_context():
        row = await db.first(
            select(EventBenefit)
            .where(EventBenefit.state.in_(["pending", "uncertain"]), EventBenefit.next_attempt_at <= utcnow())
            .order_by(EventBenefit.next_attempt_at, EventBenefit.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if row is None:
            return False
        try:
            if row.original.get("issuance_protocol") == 1:
                result = await skills.apply_xp_benefit(row.id, row.request)
            else:
                # Pre-provenance pending/uncertain records stay evidenced; no
                # missing journal/receipt inference authorizes a new XP effect.
                result = {
                    "state": "review",
                    "reason": "Prospective earning provenance unavailable",
                    "previous_effect": "unknown",
                    "entitlement_forfeited": False,
                }
        except Exception:
            result = {"state": "uncertain", "reason": "Exact remote outcome unavailable; retry original operation"}
        state = result.get("state")
        row.state = (
            "applied" if state == "applied" else "review" if state in {"recipient_erased", "review"} else "uncertain"
        )
        row.attempts += 1
        row.receipt = result
        row.next_attempt_at = utcnow() + timedelta(seconds=30)
        await db.add(
            EventBenefitObservation(benefit_id=row.id, attempt=row.attempts, observed_at=utcnow(), result=result)
        )
    return True


async def recover() -> None:
    for _ in range(100):
        if not await dispatch_one():
            return


async def export(user_id: str) -> dict[str, Any]:
    rows = await db.all(
        select(EventBenefit).where(or_(EventBenefit.user_id == user_id, EventBenefit.source_subject == user_id))
    )
    observations = await db.all(
        select(EventBenefitObservation).where(EventBenefitObservation.benefit_id.in_([r.id for r in rows]))
    )
    return {
        "earnings": [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows],
        "observations": [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in observations],
    }
