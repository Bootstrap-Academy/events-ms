from api import models
from api.database import db, filter_by, select
from api.models.booking_payment import CommercialErasureReceipt, CommercialHandoff
from api.schemas import user_export as schemas
from api.services import benefits, booking_contracts, event_cancellations, ordinary_cancellations, retained_events


def _slot(slot: models.Slot, include_link: bool = True) -> schemas.Slot:
    """Serialize a slot without the user id of the other party."""

    return schemas.Slot(
        id=slot.id,
        start=slot.start,
        end=slot.end,
        booked=slot.booked,
        event_type=slot.event_type.value if slot.event_type else None,
        skill_id=slot.skill_id,
        student_coins=slot.student_coins,
        instructor_coins=slot.instructor_coins,
        link=slot.link if include_link else None,
    )


def _rating(rating: models.LecturerRating) -> schemas.LecturerRating:
    """Serialize a rating without the user ids of the lecturer and the participant."""

    return schemas.LecturerRating(
        id=rating.id,
        skill_id=rating.skill_id,
        webinar_timestamp=rating.webinar_timestamp,
        webinar_name=rating.webinar_name,
        rating=rating.rating,
    )


async def export_user_data(user_id: str) -> schemas.UserDataExport:
    """
    Collect everything this service stores about a user.

    Only rows that belong to the given user are read and the user ids of other people are left out, so the export
    never contains anybody else's data. Has to be called inside a database context.
    """

    erasure = await db.get(CommercialErasureReceipt, subject=user_id)
    return schemas.UserDataExport(
        ordinary_event_cancellations=await ordinary_cancellations.export(user_id),
        retained_event_rights=await retained_events.export(user_id),
        event_cancellations=await event_cancellations.export(user_id),
        event_benefits=await benefits.export(user_id),
        commercial_erasure_receipt=(
            {
                "subject": erasure.subject,
                "observed_at": erasure.observed_at,
                "canonical": erasure.canonical,
                "erased_at": erasure.erased_at,
                "acknowledged_at": erasure.acknowledged_at,
            }
            if erasure is not None
            else None
        ),
        commercial_handoffs=[
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in await db.all(
                select(CommercialHandoff).where(
                    CommercialHandoff.claim_id.in_(
                        select(models.SettlementClaim.id).where(models.SettlementClaim.user_id == user_id)
                    )
                )
            )
        ],
        purchase_contracts=[
            {
                **{column.name: getattr(row, column.name) for column in row.__table__.columns},
                "availability_observation": (
                    witness.proof if (witness := await db.get(models.BookingAvailability, order_id=row.id)) else None
                ),
            }
            for row in await db.all(filter_by(models.BookingContract, user_id=user_id))
        ],
        booking_payments=[
            schemas.BookingPayment(
                id=item.id,
                xp_delivery_protocol=item.xp_delivery_protocol,
                event_id=item.event_id,
                kind=item.kind,
                state=item.state,
                quoted_coins=item.quoted_coins,
                paid_coins=item.paid_coins,
                created_at=item.created_at,
                attempts=item.attempts,
                last_error=item.last_error,
                student_deletion_claim=bool(item.original.get("student_deletion_claim")),
            )
            for item in await db.all(filter_by(models.BookingPayment, user_id=user_id))
        ],
        settlement_claims=[
            schemas.SettlementClaim(
                id=item.id,
                event_id=item.event_id,
                created_at=item.created_at,
                coins=item.coins,
                resolved_at=item.resolved_at,
                entitlement=item.entitlement,
                basis=item.basis,
                cancellation_evidence=await event_cancellations.claim_evidence(item.id),
            )
            for item in await db.all(filter_by(models.SettlementClaim, user_id=user_id))
        ],
        coin_operations=[
            schemas.CoinOperation(
                id=item.id,
                event_id=item.event_id,
                coins=item.coins,
                description=item.description,
                provenance=item.provenance,
                completed_at=item.completed_at,
                attempts=item.attempts,
                last_error=item.last_error,
            )
            for item in await db.all(filter_by(models.CoinOperation, user_id=user_id))
        ],
        webinars=[
            schemas.Webinar(
                id=webinar.id,
                xp_delivery_protocol=webinar.xp_delivery_protocol,
                skill_id=webinar.skill_id,
                creation_date=webinar.creation_date,
                name=webinar.name,
                description=webinar.description,
                link=webinar.link,
                admin_link=webinar.admin_link,
                start=webinar.start,
                end=webinar.end,
                max_participants=webinar.max_participants,
                price=webinar.price,
                participants=len(webinar.participants),
            )
            async for webinar in await db.stream(filter_by(models.Webinar, creator=user_id))
        ],
        webinar_participations=[
            schemas.WebinarParticipation(
                webinar_id=participation.webinar_id,
                skill_id=participation.webinar.skill_id,
                name=participation.webinar.name,
                start=participation.webinar.start,
                paid_coins=participation.paid_coins,
                payment_id=participation.payment_id,
            )
            async for participation in await db.stream(filter_by(models.WebinarParticipant, user_id=user_id))
        ],
        slots_offered=[_slot(slot) async for slot in await db.stream(filter_by(models.Slot, user_id=user_id))],
        slots_booked=[
            _slot(
                slot,
                (
                    await booking_contracts.ready(await db.get(models.BookingPayment, id=slot.payment_id))
                    if slot.payment_id
                    else True
                ),
            )
            async for slot in await db.stream(filter_by(models.Slot, booked_by=user_id))
        ],
        weekly_slots=[
            schemas.WeeklySlot(
                id=weekly_slot.id, weekday=weekly_slot.weekday, start=weekly_slot.start, end=weekly_slot.end
            )
            async for weekly_slot in await db.stream(filter_by(models.WeeklySlot, user_id=user_id))
        ],
        coachings=[
            schemas.Coaching(skill_id=coaching.skill_id, price=coaching.price)
            async for coaching in await db.stream(filter_by(models.Coaching, user_id=user_id))
        ],
        exams=[
            schemas.Exam(skill_id=exam.skill_id)
            async for exam in await db.stream(filter_by(models.Exam, user_id=user_id))
        ],
        emergency_cancel=await models.EmergencyCancel.exists(user_id),
        lecturer_ratings_received=[
            _rating(rating) async for rating in await db.stream(filter_by(models.LecturerRating, lecturer_id=user_id))
        ],
        lecturer_ratings_requested=[
            _rating(rating)
            async for rating in await db.stream(filter_by(models.LecturerRating, participant_id=user_id))
        ],
    )
