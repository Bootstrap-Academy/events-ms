from sqlalchemy import or_

from api.database import db, filter_by, select
from api.logger import get_logger
from api.models import (
    CalendarToken,
    Coaching,
    EmergencyCancel,
    Exam,
    LecturerRating,
    SettlementBatch,
    Slot,
    Webinar,
    WebinarParticipant,
    WeeklySlot,
)
from api.services import booking_contracts, commercial, payment_claims, retained_events, settlements
from api.utils.cache import clear_cache
from api.utils.utc import utcnow


logger = get_logger(__name__)

# prefixes of cached values which are keyed on a user id
USER_CACHE_PREFIXES = ["user", "user_skills", "calendar", "lecturer_rating"]


async def delete_user_data(user_id: str) -> None:
    """
    Erase personal learning/profile data while preserving existing booked rights.

    Bare or data-only erasure is not a service cancellation. Occupied booking
    capacity and minimal original entitlement facts survive for scoped succession
    or actual contractual resolution. Only an identified original cancellation
    declaration selects cancellation handling and supplies its own receipt time.
    """

    # The canonical backend request predates core erasure. Fetch it before event
    # locks; a service processing clock is never substituted for its receipt.
    receipt = await commercial.erasure_receipt(user_id)
    guard = await retained_events.lock_subject(user_id)
    guard.deleted = True
    grant_events = await retained_events.grant_event_ids(user_id)
    committed_bookings = await retained_events.booking_event_ids(user_id)
    requested_at = commercial.received_at(receipt)
    batch = await db.first(filter_by(SettlementBatch, kind="deletion", actor_id=user_id))
    if batch is None:
        batch = await settlements.new_batch("deletion", user_id)
    basis = {
        "request_id": batch.id,
        "request_kind": "account_erasure",
        "receipt_source": "canonical_backend" if requested_at else "original_receipt_unknown",
        "canonical_request_id": ((receipt.canonical or {}).get("request") or {}).get("id"),
        "request_received_at": requested_at.isoformat() if requested_at else None,
        "events_observed_at": receipt.observed_at.isoformat(),
    }
    counts = dict.fromkeys(
        [
            Webinar.__tablename__,
            WebinarParticipant.__tablename__,
            Slot.__tablename__,
            WeeklySlot.__tablename__,
            Coaching.__tablename__,
            Exam.__tablename__,
            EmergencyCancel.__tablename__,
            LecturerRating.__tablename__,
            CalendarToken.__tablename__,
        ],
        0,
    )
    from api.models.booking_contract import BookingContract

    cancelled_bookings = refunds = 0
    now = utcnow()
    # All cross-account erasures take the same parent ordering. Contract/payment
    # writers take their parent first too; closing contracts before these locks
    # would invert L1 admission and permit a booking to disappear without evidence.
    booked_webinars = select(WebinarParticipant.webinar_id).where(WebinarParticipant.user_id == user_id)
    webinars = await db.all(
        select(Webinar)
        .where(
            or_(
                Webinar.creator == user_id,
                Webinar.id.in_(booked_webinars),
                Webinar.id.in_(grant_events),
                Webinar.id.in_(committed_bookings),
            )
        )
        .order_by(Webinar.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    slots = await db.all(
        select(Slot)
        .where(
            or_(
                Slot.user_id == user_id,
                Slot.booked_by == user_id,
                Slot.id.in_(grant_events),
                Slot.id.in_(committed_bookings),
            )
        )
        .order_by(Slot.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    for webinar in webinars:
        keep_webinar = False
        participants = await db.all(
            filter_by(WebinarParticipant, webinar_id=webinar.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        for booked in participants:
            if webinar.creator != user_id and booked.user_id != user_id:
                continue
            payment = await commercial.payment_for_erasure(booked, "webinar", webinar.id, booked.user_id)
            facts = await commercial.retain_event(payment, webinar, webinar.creator)
            role = "instructor" if webinar.creator == user_id else "participant"
            if await retained_events.preserve_on_erasure(user_id, receipt, payment, webinar, role):
                keep_webinar = True
                if webinar.creator == user_id:
                    webinar.closed_to_new_bookings = True
                continue
            declaration = retained_events.cancellation_declaration(receipt.canonical, payment.id)
            assert declaration is not None
            cancellation_time = retained_events.declaration_time(declaration)
            cancellation_basis = basis | {
                "request_kind": "identified_service_cancellation",
                "cancellation_declaration": (
                    retained_events.declaration_basis(declaration, payment.id) if declaration else None
                ),
                "request_received_at": cancellation_time.isoformat(),
            }
            if webinar.creator == user_id:
                future = webinar.start >= cancellation_time
                await payment_claims.credit(
                    batch.id,
                    webinar.id,
                    payment.user_id,
                    [payment],
                    "Identified provider cancellation",
                    False,
                    entitlement="established" if future else "pending_evidence",
                    basis=cancellation_basis
                    | {
                        "event_start": webinar.start.isoformat(),
                        "cancellation_by": "provider",
                        "assessment": "service_not_provided" if future else "actual_performance_review",
                    },
                )
                refunds += 1
                if not future:
                    await payment_claims.credit(
                        batch.id,
                        webinar.id,
                        facts["instructor_id"],
                        [payment],
                        "Accrued webinar remuneration",
                        True,
                        field="payout_coins",
                        entitlement="pending_evidence",
                        basis=cancellation_basis
                        | {
                            "event_start": webinar.start.isoformat(),
                            "event_end": webinar.end.isoformat(),
                            "assessment": "actual_performance_and_remuneration_review",
                        },
                    )
            else:
                await payment_claims.cancel_student(
                    batch.id,
                    webinar.id,
                    payment.user_id,
                    facts["instructor_id"],
                    payment,
                    webinar.start,
                    cancellation_time,
                    cancellation_basis,
                )
                cancelled_bookings += 1
            await booking_contracts.close(payment)
            counts[WebinarParticipant.__tablename__] += 1
            await db.delete(booked)
        if webinar.creator == user_id and not keep_webinar:
            counts[Webinar.__tablename__] += 1
            await db.delete(webinar)

    for slot in slots:
        # The guard header records past bookings as well as the current one. A
        # cancelled/rejected booking leaves this slot reusable by another user.
        # Recheck the current role under the parent lock before reading its
        # current payment or preserving/cancelling anyone's original right.
        if user_id not in (slot.user_id, slot.booked_by):
            continue
        if slot.booked_by is not None:
            payment = await commercial.payment_for_erasure(slot, "coaching", slot.id, slot.booked_by)
            facts = await commercial.retain_event(payment, slot, slot.user_id)
            role = "instructor" if slot.user_id == user_id else "participant"
            if await retained_events.preserve_on_erasure(user_id, receipt, payment, slot, role):
                if slot.user_id == user_id:
                    slot.weekly_slot_id = None
                    slot.weekly_slot = None
                continue
            declaration = retained_events.cancellation_declaration(receipt.canonical, payment.id)
            assert declaration is not None
            cancellation_time = retained_events.declaration_time(declaration)
            cancellation_basis = basis | {
                "request_kind": "identified_service_cancellation",
                "cancellation_declaration": (
                    retained_events.declaration_basis(declaration, payment.id) if declaration else None
                ),
                "request_received_at": cancellation_time.isoformat(),
            }
            if slot.user_id == user_id:
                future = slot.start >= cancellation_time
                await payment_claims.credit(
                    batch.id,
                    slot.id,
                    payment.user_id,
                    [payment],
                    "Identified provider cancellation",
                    False,
                    entitlement="established" if future else "pending_evidence",
                    basis=cancellation_basis
                    | {
                        "event_start": slot.start.isoformat(),
                        "cancellation_by": "provider",
                        "assessment": "service_not_provided" if future else "actual_performance_review",
                    },
                )
                refunds += 1
                if not future:
                    await payment_claims.credit(
                        batch.id,
                        slot.id,
                        facts["instructor_id"],
                        [payment],
                        "Accrued coaching remuneration",
                        True,
                        field="payout_coins",
                        entitlement="pending_evidence",
                        basis=cancellation_basis
                        | {
                            "event_start": slot.start.isoformat(),
                            "event_end": slot.end.isoformat(),
                            "assessment": "actual_performance_and_remuneration_review",
                        },
                    )
            else:
                await payment_claims.cancel_student(
                    batch.id,
                    slot.id,
                    payment.user_id,
                    facts["instructor_id"],
                    payment,
                    slot.start,
                    cancellation_time,
                    cancellation_basis,
                )
                cancelled_bookings += 1
            await booking_contracts.close(payment)
        if slot.user_id == user_id:
            counts[Slot.__tablename__] += 1
            await db.delete(slot)
        else:
            slot.cancel()

    await commercial.preserve_detached_claims(user_id, receipt, batch.id)
    await retained_events.withdraw_grants(user_id)

    # Orphaned/pending offers are retained for reconciliation, never delivered
    # after erasure. Parent-owned contracts above were closed under parent locks.
    for contract in await db.all(filter_by(BookingContract, user_id=user_id)):
        if await retained_events.preserved(contract.id, user_id):
            continue
        contract.closed = True
        if contract.state not in ("offered", "failed"):
            contract.state = "review"
    receipt.erased_at = receipt.erased_at or now

    weekly_slot: WeeklySlot
    async for weekly_slot in await db.stream(filter_by(WeeklySlot, user_id=user_id)):
        counts[WeeklySlot.__tablename__] += 1
        await db.delete(weekly_slot)

    coaching: Coaching
    async for coaching in await db.stream(filter_by(Coaching, user_id=user_id)):
        counts[Coaching.__tablename__] += 1
        await db.delete(coaching)

    exam: Exam
    async for exam in await db.stream(filter_by(Exam, user_id=user_id)):
        counts[Exam.__tablename__] += 1
        await db.delete(exam)

    emergency_cancel: EmergencyCancel
    async for emergency_cancel in await db.stream(filter_by(EmergencyCancel, user_id=user_id)):
        counts[EmergencyCancel.__tablename__] += 1
        await db.delete(emergency_cancel)

    # ratings the user has received as a lecturer as well as ratings they have not submitted yet
    rating: LecturerRating
    async for rating in await db.stream(
        select(LecturerRating).where(
            or_(LecturerRating.lecturer_id == user_id, LecturerRating.participant_id == user_id)
        )
    ):
        counts[LecturerRating.__tablename__] += 1
        await db.delete(rating)

    # the token of the ics calendar feed, which stops working as soon as it is deleted
    calendar_token: CalendarToken
    async for calendar_token in await db.stream(filter_by(CalendarToken, user_id=user_id)):
        counts[CalendarToken.__tablename__] += 1
        await db.delete(calendar_token)

    for prefix in USER_CACHE_PREFIXES:
        await clear_cache(prefix)

    batches = await db.all(filter_by(SettlementBatch, kind="deletion", actor_id=user_id))
    await settlements.finish([item.id for item in batches])
    await commercial.acknowledge_erasure(user_id)

    logger.info(
        "deleted user data (%s, cancelled bookings: %s, refunds: %s)",
        ", ".join(f"{table}: {cnt}" for table, cnt in counts.items()),
        cancelled_bookings,
        refunds,
    )
