"""Existing contract scope and actual erasure declarations, never inferred forfeiture."""

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from api.database import db, filter_by, select
from api.models.retained_event import EventRightGrant, EventSubjectGuard, RetainedEventErasure, RetainedEventRight
from api.utils.utc import utcnow


async def lock_subject(subject: str) -> EventSubjectGuard:
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError

    try:
        async with db.session.begin_nested():
            await db.exec(insert(EventSubjectGuard).values(subject=subject, deleted=False))
    except IntegrityError:
        pass
    guard = await db.first(
        filter_by(EventSubjectGuard, subject=subject).with_for_update().execution_options(populate_existing=True)
    )
    assert guard is not None
    return guard


async def require_current_subject(subject: str) -> EventSubjectGuard:
    """Local creation/admission fence shared with erasure, before parent locks."""
    from fastapi import HTTPException

    guard = await lock_subject(subject)
    if guard.deleted:
        raise HTTPException(409, "This service subject was erased; existing rights remain")
    return guard


async def grant_event_ids(subject: str) -> list[str]:
    """Committed parent discovery after the target guard, without child locks.

    New delivery shares that guard, so no target grant can be added while this
    read runs. Cancellation may withdraw a committed grant meanwhile: including
    its old parent is safe, and actual grant mutation still follows parent locks.
    The bounded reserved reader avoids both an old RR snapshot and grant→parent
    lock inversion against cancellation's parent→grant order.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession

    query = select(EventRightGrant.request).where(
        EventRightGrant.subject == subject, EventRightGrant.state == "granted"
    )
    if db.engine.dialect.name == "sqlite":
        # Local sequential test engine; supported concurrent deployments use the
        # separately reserved committed reader below.
        requests = (await db.exec(query)).scalars().all()
    else:
        if db.committed_read_engine is None:
            raise RuntimeError("Committed grant discovery reader was not reserved at startup")
        async with (
            asyncio.timeout(5),
            AsyncSession(db.committed_read_engine, autoflush=False, expire_on_commit=False) as session,
        ):
            requests = (await session.execute(query)).scalars().all()
    return [request["original_scope"]["event_id"] for request in requests]


async def record_booking_reservation(payment: Any) -> None:
    """Add discovery to the exact subject guard already owned by this producer.

    Original acceptance/payment facts remain in their existing records. This
    header commits with occupied capacity, without a second indexed range lock.
    """
    from fastapi import HTTPException

    guard = await db.first(
        filter_by(EventSubjectGuard, subject=payment.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if guard is None or guard.deleted:
        raise HTTPException(409, "Current booking subject unavailable")
    entries = dict(guard.booking_reservations or {})
    value = {"event_id": payment.event_id, "kind": payment.kind}
    if payment.id in entries and entries[payment.id] != value:
        raise HTTPException(409, "Conflicting original booking reservation")
    entries[payment.id] = value
    guard.booking_reservations = entries


async def booking_event_ids(subject: str) -> list[str]:
    """Read the owned exact guard row currently, without nested connection/gaps.

    A new reservation committed while erasure waited is visible here even when
    an earlier receipt read established an old repeatable-read snapshot. Actual
    financial children remain locked after their ordered parents.
    """
    guard = await db.first(
        filter_by(EventSubjectGuard, subject=subject).with_for_update().execution_options(populate_existing=True)
    )
    assert guard is not None
    return [entry["event_id"] for entry in (guard.booking_reservations or {}).values()]


async def withdraw_grants(subject: str) -> None:
    for grant in await db.all(
        filter_by(EventRightGrant, subject=subject, state="granted")
        .with_for_update()
        .execution_options(populate_existing=True)
    ):
        grant.state = "withdrawn"


def cancellation_declaration(canonical: dict[str, Any] | None, payment_id: str) -> dict[str, Any] | None:
    """Only an independently recorded, identified declaration supplies cancellation.

    Current data-only intake explicitly selects no such action. Historical bare
    erasure receipts remain unknown. The time belongs to the actual declaration,
    never to later execution or a case's first unrelated request.
    """
    if not isinstance(canonical, dict):
        return None
    request = canonical.get("request")
    if not isinstance(request, dict):
        return None
    evidence = request.get("evidence")
    if not isinstance(evidence, dict):
        return None
    declaration = evidence.get("declaration")
    if not isinstance(declaration, dict):
        return None
    if (
        request.get("source") not in {"authenticated_service_receipt", "verified_earlier_declaration"}
        or declaration.get("paid_contract_intent") != "cancel_identified_contracts"
        or not isinstance(declaration.get("contract_ids"), list)
        or payment_id not in declaration["contract_ids"]
        or not isinstance(declaration.get("original_text"), str)
        or not declaration["original_text"].strip()
    ):
        return None
    received = declaration.get("received_at")
    if not isinstance(received, str):
        return None
    try:
        parsed = datetime.fromisoformat(received.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return {"request_id": request.get("id"), "received_at": received, "declaration": declaration}


def declaration_time(declaration: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(declaration["received_at"].replace("Z", "+00:00"))


def declaration_basis(declaration: dict[str, Any], payment_id: str) -> dict[str, Any]:
    # Other selected contracts and arbitrary original text are not copied into
    # a counterparty's financial export. The owner's canonical original remains.
    return {
        "request_id": declaration["request_id"],
        "received_at": declaration["received_at"],
        "paid_contract_intent": "cancel_identified_contracts",
        "contract_id": payment_id,
    }


async def right_for(payment_id: str, role: str) -> RetainedEventRight | None:
    from api.services.payment_claims import committed_and_local_keys

    # The owning event/payment serializes creation of this identity. Rights are
    # retained, so committed/local IDs cannot become absent participant seats.
    keys = await committed_and_local_keys(
        select(RetainedEventRight.id).where(
            RetainedEventRight.payment_id == payment_id, RetainedEventRight.role == role
        )
    )
    if not keys:
        return None
    if len(keys) != 1:
        raise ValueError("Conflicting original event rights")
    (right_id,) = next(iter(keys))
    right = await db.first(
        filter_by(RetainedEventRight, id=right_id).with_for_update().execution_options(populate_existing=True)
    )
    if right is None or right.payment_id != payment_id or right.role != role:
        raise ValueError("Original event right identity changed")
    return right


async def preserve_on_erasure(subject: str, receipt: Any, payment: Any, event: Any, role: str) -> bool:
    """Called under the existing parent lock, before any participation deletion.

    Keeping occupied capacity is not a claim of delivery, new personal access or
    financial satisfaction. A separately elected scoped successor is required
    for fresh use after erasure. Old replay cannot withdraw a newer recipient.
    """
    if cancellation_declaration(receipt.canonical, payment.id) is not None:
        return False
    from api.services import booking_contracts

    right = await right_for(payment.id, role)
    if right is None:
        observed = utcnow()
        original = {
            "source": "existing_booking_observed_before_erasure",
            "payment_id": payment.id,
            "event_id": payment.event_id,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "kind": payment.kind,
            "role": role,
            "payment_state": payment.state,
            "paid_coins": payment.paid_coins,
            "admission_observed": await booking_contracts.ready(payment),
            "payment_evidence_sha256": hashlib.sha256(
                json.dumps(
                    {"original": payment.original, "evidence": payment.evidence}, sort_keys=True, default=str
                ).encode()
            ).hexdigest(),
            "payment_or_performance_inferred": False,
        }
        right = await db.add(
            RetainedEventRight(
                id=str(uuid5(NAMESPACE_URL, f"bootstrap-event-right:{payment.id}:{role}")),
                source_subject=subject,
                event_id=payment.event_id,
                payment_id=payment.id,
                kind=payment.kind,
                role=role,
                observed_at=observed,
                original=original,
                current_subject=None,
                state="preserved",
            )
        )
    elif right.current_subject not in (None, subject):
        # An already succeeded right is not cancelled by delayed old-account work.
        return True
    else:
        right.current_subject = None
        if right.state == "active":
            right.state = "preserved"
    canonical_id = ((receipt.canonical or {}).get("request") or {}).get("id")
    identity = canonical_id or f"local-observation:{receipt.observed_at.isoformat()}"
    erasure_id = str(uuid5(NAMESPACE_URL, f"bootstrap-event-right-erasure:{right.id}:{subject}:{identity}"))
    if await db.get(RetainedEventErasure, id=erasure_id) is None:
        await db.add(
            RetainedEventErasure(
                id=erasure_id,
                right_id=right.id,
                subject=subject,
                observed_at=utcnow(),
                receipt={
                    "canonical": receipt.canonical,
                    "events_observed_at": receipt.observed_at.isoformat(),
                    "paid_cancellation_inferred": False,
                },
            )
        )
    return True


async def preserved(payment_id: str, subject: str | None = None) -> bool:
    # Reevaluate mutable state/ownership after the owning payment wait. A stale
    # snapshot of an active or cancelled right cannot decide detached claims.
    for role in ("participant", "instructor"):
        right = await right_for(payment_id, role)
        if (
            right is not None
            and right.state in {"preserved", "active", "resolution_pending"}
            and (subject is None or subject in (right.source_subject, right.current_subject))
        ):
            return True
    return False


async def export(subject: str) -> dict[str, Any]:
    from sqlalchemy import or_

    erased = select(RetainedEventErasure.right_id).where(RetainedEventErasure.subject == subject)
    rights = await db.all(
        select(RetainedEventRight).where(
            or_(
                RetainedEventRight.source_subject == subject,
                RetainedEventRight.current_subject == subject,
                RetainedEventRight.id.in_(erased),
            )
        )
    )
    guard = await db.get(EventSubjectGuard, subject=subject)
    return {
        "rights": [{column.name: getattr(row, column.name) for column in row.__table__.columns} for row in rights],
        "erasures": [
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in await db.all(filter_by(RetainedEventErasure, subject=subject))
        ],
        "grants": [
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in await db.all(
                select(EventRightGrant).where(
                    or_(
                        EventRightGrant.subject == subject,
                        EventRightGrant.right_id.in_([r.id for r in rights if r.source_subject == subject]),
                    )
                )
            )
        ],
        "booking_reservations": dict(guard.booking_reservations or {}) if guard else {},
        "subject_guard": {"subject": guard.subject, "deleted": guard.deleted} if guard else None,
    }


def original(right: RetainedEventRight) -> dict[str, Any]:
    return {
        "id": right.id,
        "source_subject": right.source_subject,
        "event_id": right.event_id,
        "payment_id": right.payment_id,
        "kind": right.kind,
        "role": right.role,
        "observed_at": right.observed_at.isoformat(),
        "original": right.original,
    }


async def list_rights(source_subject: str) -> list[dict[str, Any]]:
    return [
        original(right) | {"current_subject": right.current_subject, "state": right.state}
        for right in await db.all(
            filter_by(RetainedEventRight, source_subject=source_subject).order_by(RetainedEventRight.id)
        )
    ]


async def get_original(source_subject: str, right_id: str) -> dict[str, Any]:
    from fastapi import HTTPException

    right = await db.first(filter_by(RetainedEventRight, id=right_id, source_subject=source_subject))
    if right is None:
        raise HTTPException(404, "Existing event right unavailable for this source")
    return original(right)


async def successor_authority(source_subject: str, grant_id: str) -> dict[str, Any] | None:
    from fastapi import HTTPException
    from httpx import HTTPError

    from api.services.internal import InternalService

    try:
        async with InternalService.SHOP.client as client:
            client.event_hooks["response"] = []
            response = await client.post(
                "/claims/event_successor_authority", json={"grant_id": grant_id, "source_subject": source_subject}
            )
        if response.status_code != 200:
            raise HTTPException(503, "Current event continuation admission unavailable")
        value = response.json()
        if value is None:
            return None
        if (
            not isinstance(value, dict)
            or value.get("id") != grant_id
            or value.get("source") != "events"
            or value.get("purpose") != "existing_event_continuation"
            or value.get("new_purchase") is not False
            or value.get("claimant_authorization", {}).get("source_subject") != source_subject
        ):
            raise HTTPException(503, "Invalid event continuation admission")
        return value
    except (HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "Current event continuation admission unavailable") from None


def delivery_result(grant: EventRightGrant) -> dict[str, Any]:
    return {
        "grant_id": grant.id,
        "right_id": grant.right_id,
        "subject": grant.subject,
        "state": grant.state,
        "original_result": grant.result,
        "new_purchase": False,
    }


async def webinar_seats(event_id: str, payment_id: str, subject: str) -> tuple[Any, Any]:
    """Read current original/destination seats under the target and event locks.

    Fresh committed keys plus actual own flush changes handle deletion/rekey;
    unioning an old snapshot would resurrect absent PKs and their index gaps.
    The existing reserved nonlocking reader stays within the connection budget.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession

    from api.models.webinar_participants import WebinarParticipant, own_seat_changes

    await db.session.flush()
    query = (
        select(WebinarParticipant.webinar_id)
        .add_columns(WebinarParticipant.user_id, WebinarParticipant.payment_id)
        .where(WebinarParticipant.webinar_id == event_id)
    )
    if db.engine.dialect.name == "sqlite":
        rows = (await db.exec(query)).all()
    else:
        if db.committed_read_engine is None:
            raise RuntimeError("Committed seat discovery reader was not reserved at startup")
        async with (
            asyncio.timeout(5),
            AsyncSession(db.committed_read_engine, autoflush=False, expire_on_commit=False) as session,
        ):
            rows = (await session.execute(query)).all()
    keys = {(event, user): (payment,) for event, user, payment in rows}
    for key, value in own_seat_changes(db.session.sync_session).items():
        if key[0] == event_id:
            if value is None:
                keys.pop(key, None)
            else:
                keys[key] = value
    booked = other = None
    for (event, user), (payment,) in sorted(keys.items()):
        if payment != payment_id and user != subject:
            continue
        seat = await db.first(
            filter_by(WebinarParticipant, webinar_id=event, user_id=user)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if seat is None or (seat.webinar_id, seat.user_id, seat.payment_id) != (event, user, payment):
            raise ValueError("Current webinar occupancy changed under its owning parent")
        if payment == payment_id:
            if booked is not None:
                raise ValueError("Conflicting original webinar seats")
            booked = seat
        if user == subject:
            other = seat
    return booked, other


async def deliver(source_subject: str, grant_id: str) -> dict[str, Any]:
    from fastapi import HTTPException

    from api.models import BookingPayment, Slot, Webinar
    from api.services import booking_contracts

    prior = await db.first(filter_by(EventRightGrant, id=grant_id))
    if prior is not None:
        await get_original(source_subject, prior.right_id)
        await lock_subject(prior.subject)
        prior = await db.first(
            filter_by(EventRightGrant, id=grant_id).with_for_update().execution_options(populate_existing=True)
        )
        assert prior is not None
        return delivery_result(prior)
    authority = await successor_authority(source_subject, grant_id)
    if authority is None:
        raise HTTPException(409, "Current continuation election unavailable; original rights remain")
    subject = str(authority["successor"])
    guard = await lock_subject(subject)
    from api.services.payment_claims import committed_and_local_keys

    # Distinct target guards do not serialize an empty grant-index interval.
    # Discover committed/own keys first, then refresh only an existing receipt.
    keys = await committed_and_local_keys(select(EventRightGrant.id).where(EventRightGrant.id == grant_id))
    prior = (
        await db.first(
            filter_by(EventRightGrant, id=grant_id).with_for_update().execution_options(populate_existing=True)
        )
        if keys
        else None
    )
    if prior is not None:
        await get_original(source_subject, prior.right_id)
        return delivery_result(prior)
    if guard.deleted:
        raise HTTPException(409, "Target learning data was erased; existing event right remains")
    current = await successor_authority(source_subject, grant_id)
    if current is None or current["successor"] != subject or current["original_scope"] != authority["original_scope"]:
        raise HTTPException(409, "Continuation admission changed while waiting")
    scope = authority["original_scope"]
    model = Webinar if scope.get("kind") == "webinar" else Slot
    event = await db.first(
        filter_by(model, id=scope["event_id"]).with_for_update().execution_options(populate_existing=True)
    )
    right = await db.first(
        filter_by(RetainedEventRight, id=authority["original_contract"], source_subject=source_subject)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        right is None
        or original(right) != scope
        or right.state not in {"preserved", "active"}
        or right.current_subject not in (None, subject)
        or event is None
        or event.end <= utcnow()
        or event.start.isoformat() != right.original["start"]
        or event.end.isoformat() != right.original["end"]
    ):
        raise HTTPException(409, "Exact existing event supply requires resolution; original right remains")
    payment = await db.get(BookingPayment, id=right.payment_id)
    if (
        payment is None
        or payment.event_id != right.event_id
        or not await booking_contracts.ready(payment, continuation=True)
    ):
        raise HTTPException(409, "Existing booking confirmation or access requires resolution")
    former = {source_subject, subject} | {
        r.subject for r in await db.all(filter_by(RetainedEventErasure, right_id=right.id))
    }
    affected = [right]
    if right.role == "instructor":
        provider = event.creator if right.kind == "webinar" else event.user_id
        if provider not in former:
            raise HTTPException(409, "Current provider mapping requires resolution")
        if right.kind == "webinar":
            # Hosting one original webinar covers its existing participants. It
            # does not reopen offers or transfer their separate financial rights.
            affected = await db.all(
                filter_by(RetainedEventRight, event_id=right.event_id, role="instructor", source_subject=source_subject)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if any(r.current_subject not in (None, subject) for r in affected):
                raise HTTPException(409, "Existing webinar hosting is already in use")
            event.creator = subject
        else:
            if event.payment_id != payment.id:
                raise HTTPException(409, "Original occupied coaching capacity changed")
            event.user_id = subject
    elif right.role == "participant":
        if right.kind == "webinar":
            booked, other = await webinar_seats(right.event_id, payment.id, subject)
            if booked is None or booked.user_id not in former or (other is not None and other.payment_id != payment.id):
                raise HTTPException(409, "Existing webinar seat requires resolution; no capacity was consumed")
            booked.user_id = subject
        else:
            if event.payment_id != payment.id or event.booked_by not in former:
                raise HTTPException(409, "Original occupied coaching capacity changed")
            event.booked_by = subject
    else:
        raise HTTPException(409, "Unknown original event role")
    for item in affected:
        item.current_subject = subject
        item.state = "active"
    result = {
        "event_id": right.event_id,
        "kind": right.kind,
        "role": right.role,
        "access_granted": True,
        "original_start": event.start.isoformat(),
        "original_end": event.end.isoformat(),
        "affected_right_ids": sorted(r.id for r in affected),
        "new_purchase": False,
        "new_terms_accepted": False,
        "original_performance_inferred": False,
        "original_scope": scope,
    }
    grant = await db.add(
        EventRightGrant(
            id=grant_id,
            right_id=right.id,
            subject=subject,
            state="granted",
            created_at=utcnow(),
            request={
                "source_subject": source_subject,
                "original_scope": scope,
                "backend_grant_id": grant_id,
                "successor": subject,
            },
            result=result,
        )
    )
    return delivery_result(grant)
