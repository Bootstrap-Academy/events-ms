"""Scoped existing-event use without ordinary login or financial spending authority."""

from datetime import timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from httpx import HTTPError

from api.database import db, filter_by
from api.endpoints.closed_offerings import closed_calendar, closed_offering
from api.models import BookingPayment, EventRightGrant, RetainedEventRight, Slot, Webinar, WebinarParticipant
from api.schemas.user import User
from api.services import booking_contracts, retained_events
from api.services.internal import InternalService
from api.utils.utc import utcnow


router = APIRouter(prefix="/learning")
learning_key = APIKeyHeader(name="x-learning-key", auto_error=False, scheme_name="RetainedLearning")


async def learning_subject(digest: str) -> User:
    try:
        async with InternalService.SHOP.client as client:
            client.event_hooks["response"] = []
            response = await client.post("/claims/learning_authority_digest", json={"hash": digest})
        if response.status_code in (401, 403):
            raise HTTPException(401, "Limited learning authority unavailable")
        if response.status_code != 200:
            raise HTTPException(503, "Current learning authority unavailable")
        value = response.json()
        if value is None:
            raise HTTPException(401, "Limited learning authority unavailable")
        if (
            not isinstance(value, dict)
            or value.get("purpose") != "retained_learning"
            or value.get("ordinary_authority") is not False
            or value.get("financial_authority") is not False
            or value.get("admin") is not False
            or value.get("email_verified") is not True
        ):
            raise HTTPException(503, "Invalid scoped authority response")
        return User(id=str(UUID(value["subject"])), email_verified=True, admin=False)
    except (HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "Current learning authority unavailable") from None


async def learning_auth(key: str | None = Security(learning_key)) -> User:
    if key is None or not 43 <= len(key) <= 256:
        raise HTTPException(401, "Limited learning credential required")
    digest = sha256(key.encode()).hexdigest()
    user = await learning_subject(digest)
    guard = await retained_events.lock_subject(user.id)
    if guard.deleted:
        raise HTTPException(401, "This service subject was erased; existing rights remain")
    current = await learning_subject(digest)
    if current.id != user.id:
        raise HTTPException(503, "Scoped subject changed during admission")
    return current


async def existing_events(subject: str, event_id: str | None = None) -> list[dict[str, Any]]:
    grants = await db.all(filter_by(EventRightGrant, subject=subject, state="granted"))
    right_ids = {grant.right_id for grant in grants}
    rights = await db.all(
        filter_by(RetainedEventRight, current_subject=subject, state="active").order_by(
            RetainedEventRight.event_id, RetainedEventRight.id
        )
    )
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for right in rights:
        identity = (right.event_id, right.role)
        if event_id is not None and event_id != right.event_id:
            continue
        # A host may bind several original participant obligations, but only an
        # exact currently granted right supplies this subject's access evidence.
        if right.id not in right_ids:
            continue
        model: type[Webinar] | type[Slot] = Webinar if right.kind == "webinar" else Slot
        event = await db.first(
            filter_by(model, id=right.event_id).with_for_update().execution_options(populate_existing=True)
        )
        payment = await db.get(BookingPayment, id=right.payment_id)
        now = utcnow()
        bound = False
        if event is not None:
            if right.role == "instructor":
                bound = (event.creator if right.kind == "webinar" else event.user_id) == subject
            elif right.kind == "webinar":
                bound = (
                    await db.first(
                        filter_by(
                            WebinarParticipant, webinar_id=right.event_id, payment_id=right.payment_id, user_id=subject
                        )
                    )
                    is not None
                )
            else:
                bound = event.booked_by == subject and event.payment_id == right.payment_id
        usable = (
            event is not None
            and bound
            and now < event.end
            and event.start.isoformat() == right.original["start"]
            and event.end.isoformat() == right.original["end"]
            and await booking_contracts.ready(payment)
        )
        in_window = usable and event.start - now < timedelta(days=1)
        candidate = {
            "right_id": right.id,
            "event_id": right.event_id,
            "kind": right.kind,
            "role": right.role,
            "state": "available" if usable else "resolution_required",
            "start": right.original["start"],
            "end": right.original["end"],
            "title": event.name if isinstance(event, Webinar) else None,
            "skill_id": event.skill_id if event else None,
            "link": (event.admin_link if right.role == "instructor" else event.link) if in_window else None,
            "new_purchase": False,
            "new_terms_accepted": False,
        }
        # Preserve a usable exact election if another independently elected
        # right for the same host/session now needs resolution. Never replace it
        # with a sibling that has no personal continuation grant.
        if identity not in result or (result[identity]["state"] != "available" and usable):
            result[identity] = candidate
    return list(result.values())


@router.get("/events")
async def list_events(user: User = Depends(learning_auth)) -> list[dict[str, Any]]:
    return await existing_events(user.id)


@router.get("/events/{event_id}")
async def get_event(event_id: UUID, user: User = Depends(learning_auth)) -> list[dict[str, Any]]:
    result = await existing_events(user.id, str(event_id))
    if not result:
        raise HTTPException(404, "Existing event access unavailable for this subject")
    return result


@router.get("/calendar", dependencies=[closed_calendar], deprecated=True)
async def calendar(user: User = Depends(learning_auth)) -> dict[str, Any]:
    from api.endpoints.calendar import get_events

    access = await existing_events(user.id)
    events = await get_events(
        user.id, False, None, None, None, None, None, None, None, None, None, None, None, None, None
    )
    for response in events:
        model: type[Webinar] | type[Slot] = Webinar if response.type.value == "webinar" else Slot
        event = await db.get(model, id=response.id)
        apply_scoped_links(response, event, user.id, access)
    return {"events": events}


def apply_scoped_links(response: Any, event: Any, subject: str, access: list[dict[str, Any]]) -> None:
    """Use the same exact retained election for every scoped link presentation.

    Ordinary host identity is not a continuation election. The owning read above
    has already checked the elected original, its readiness, current binding and
    original window. Ordinary newly booked participant readiness stays in the
    existing handler; retained participant views use that same exact decision.
    """
    host = event is not None and (event.creator if isinstance(event, Webinar) else event.user_id) == subject
    role = "instructor" if host else "participant"
    view = next((item for item in access if item["event_id"] == response.id and item["role"] == role), None)
    if host:
        response.admin_link = view["link"] if view else None
        response.link = event.link if view and view["link"] is not None else None
    elif view is not None:
        response.admin_link = None
        response.link = view["link"]


@router.get("/webinars/{webinar_id}")
async def webinar(webinar_id: UUID, user: User = Depends(learning_auth)) -> Any:
    from api.endpoints import webinars

    access = await existing_events(user.id, str(webinar_id))
    event = await webinars.get_webinar.dependency(str(webinar_id))
    response = await webinars.get_webinar_by_id(event, user)
    apply_scoped_links(response, event, user.id, access)
    return response


@router.post("/webinars/{webinar_id}/offer", dependencies=[closed_offering], deprecated=True)
async def webinar_offer(webinar_id: UUID, user: User = Depends(learning_auth)) -> Any:
    from api.endpoints import webinars

    event = await webinars.get_webinar.dependency(str(webinar_id))
    return await webinars.webinar_offer(event, user)


@router.post("/webinars/{webinar_id}/participants", dependencies=[closed_offering], deprecated=True)
async def book_webinar(
    data: booking_contracts.Acceptance, webinar_id: UUID, user: User = Depends(learning_auth)
) -> Any:
    from api.endpoints import webinars

    event = await webinars.get_webinar.dependency(str(webinar_id))
    # Exact financial acceptance remains separately authorized in the backend.
    return await webinars.register_for_webinar(data, event, user)


@router.post("/coachings/{skill_id}/{slot_id}/offer", dependencies=[closed_offering], deprecated=True)
async def coaching_offer(skill_id: str, slot_id: UUID, user: User = Depends(learning_auth)) -> Any:
    from api.endpoints import coachings

    return await coachings.coaching_offer(skill_id, str(slot_id), user)


@router.post("/coachings/{skill_id}/{slot_id}", dependencies=[closed_offering], deprecated=True)
async def book_coaching(
    data: booking_contracts.Acceptance, skill_id: str, slot_id: UUID, user: User = Depends(learning_auth)
) -> Any:
    from api.endpoints import coachings

    return await coachings.book_coaching(data, skill_id, str(slot_id), user)
