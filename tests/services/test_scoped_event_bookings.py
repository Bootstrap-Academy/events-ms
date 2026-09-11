"""Owning booking delegates; destination purchase/confirmation is a synthetic HTTP transport."""

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db, select
from api.endpoints import learning
from api.models import BookingContract, BookingPayment, EventSubjectGuard, WebinarParticipant
from api.services import booking_contracts
from tests.endpoints import test_webinars
from tests.endpoints.test_webinars import STUDENT, _user, _webinar
from tests.required import required


async def test_scoped_webinar_offer_acceptance_uses_same_exact_contract(
    session: AsyncSession, spend_coins: AsyncMock
) -> None:
    event = _webinar()
    event.id = str(uuid4())
    await db.add(event)
    offered = await learning.webinar_offer(UUID(event.id), _user(STUDENT))
    acceptance = booking_contracts.Acceptance(
        order_id=offered["offer"]["id"],
        offer_hash=offered["offer"]["hash"],
        accepted=True,
        early_performance_requested=True,
    )
    result = await learning.book_webinar(acceptance, UUID(event.id), _user(STUDENT))
    assert result.booked is True and result.bookable is False
    payments = await db.all(select(BookingPayment))
    assert len(payments) == 1 and payments[0].id == str(acceptance.order_id)
    assert payments[0].user_id == STUDENT and payments[0].paid_coins == 1000
    contract = required(await db.get(BookingContract, id=payments[0].id))
    assert contract.acceptance == acceptance.payload() and contract.state == "ready"
    assert spend_coins.await_count == 1
    guard = required(await db.get(EventSubjectGuard, subject=STUDENT))
    assert guard.booking_reservations == {payments[0].id: {"event_id": event.id, "kind": "webinar"}}
    from api.services.user_export import export_user_data

    assert (required(await export_user_data(STUDENT))).retained_event_rights[
        "booking_reservations"
    ] == guard.booking_reservations
    assert (await export_user_data(str(uuid4()))).retained_event_rights["booking_reservations"] == {}


async def test_erased_subject_cannot_place_a_new_booking_from_an_old_offer(
    session: AsyncSession, spend_coins: AsyncMock
) -> None:
    event = _webinar()
    event.id = str(uuid4())
    await db.add(event)
    offered = await learning.webinar_offer(UUID(event.id), _user(STUDENT))
    acceptance = booking_contracts.Acceptance(
        order_id=offered["offer"]["id"],
        offer_hash=offered["offer"]["hash"],
        accepted=True,
        early_performance_requested=True,
    )
    (required(await db.get(EventSubjectGuard, subject=STUDENT))).deleted = True
    await db.commit()
    with pytest.raises(HTTPException) as denied:
        await learning.book_webinar(acceptance, UUID(event.id), _user(STUDENT))
    assert denied.value.status_code == 409
    assert await db.all(select(BookingPayment)) == [] and await db.all(select(WebinarParticipant)) == []
    spend_coins.assert_not_awaited()


async def test_wrong_exact_offer_still_has_no_booking_or_debit(session: AsyncSession, spend_coins: AsyncMock) -> None:
    event = _webinar()
    event.id = str(uuid4())
    await db.add(event)
    offered = await learning.webinar_offer(UUID(event.id), _user(STUDENT))
    acceptance = booking_contracts.Acceptance(
        order_id=offered["offer"]["id"], offer_hash="different", accepted=True, early_performance_requested=True
    )
    with pytest.raises(HTTPException) as denied:
        await learning.book_webinar(acceptance, UUID(event.id), _user(STUDENT))
    assert denied.value.status_code == 409
    assert await db.all(select(BookingPayment)) == [] and await db.all(select(WebinarParticipant)) == []
    spend_coins.assert_not_awaited()


async def test_scoped_route_mounting_body_and_path_use_existing_booking_pipeline(
    session: AsyncSession, client: AsyncClient, spend_coins: AsyncMock
) -> None:
    from api.app import app

    event = _webinar()
    event.id = str(uuid4())
    await db.add(event)
    app.dependency_overrides[learning.learning_auth] = lambda: _user(STUDENT)
    try:
        offered = await client.post(f"/learning/webinars/{event.id}/offer")
        assert offered.status_code == 200
        offer = offered.json()["offer"]
        response = await client.post(
            f"/learning/webinars/{event.id}/participants",
            json={
                "order_id": offer["id"],
                "offer_hash": offer["hash"],
                "accepted": True,
                "early_performance_requested": True,
            },
        )
        assert response.status_code == 200 and response.json()["booked"] is True
        assert spend_coins.await_count == 1
        parameters = app.openapi()["paths"]["/learning/webinars/{webinar_id}/participants"]["post"]["parameters"]
        assert next(p for p in parameters if p["name"] == "webinar_id")["in"] == "path"
    finally:
        del app.dependency_overrides[learning.learning_auth]


@pytest.mark.parametrize("first_outcome", ["cancelled", "rejected"])
async def test_old_coaching_booking_header_cannot_claim_unrelated_rebooking(
    session: AsyncSession, mocker: MockerFixture, spend_coins: AsyncMock, first_outcome: str
) -> None:
    from datetime import timedelta
    from unittest.mock import AsyncMock

    from api.exceptions.coaching import NotEnoughCoinsError
    from api.models import Coaching, RetainedEventRight, SettlementClaim, Slot
    from api.schemas.ordinary_cancellation import CancellationDeclaration, CancellationPreparation
    from api.schemas.user import UserInfo
    from api.services import ordinary_cancellations
    from api.services.user_deletion import delete_user_data
    from api.services.user_export import export_user_data
    from api.utils.utc import utcnow
    from tests.services.test_retained_events import canonical

    host, second = str(uuid4()), str(uuid4())
    info = UserInfo(id=host, name="synthetic", display_name="Synthetic host", avatar_url=None)
    for path in ("api.endpoints.coachings.get_userinfo", "api.endpoints.calendar.get_userinfo"):
        mocker.patch(path, AsyncMock(return_value=info))
    for path in (
        "api.endpoints.coachings.clear_cache",
        "api.services.ordinary_cancellations.clear_cache",
        "api.services.user_deletion.clear_cache",
        "api.services.settlements.finish",
    ):
        mocker.patch(path, AsyncMock())

    receipts: dict[str, Any] = {}

    async def commercial(operation: str, payload: dict[str, Any]) -> Any:
        if operation == "erasure":
            return receipts.setdefault(payload["subject"], canonical(payload["subject"]))
        assert operation == "inventory"
        return {"accepted": True, "financial_satisfaction": False}

    mocker.patch("api.services.shop.commercial", side_effect=commercial)
    slot = required(await Slot.create(host, utcnow() + timedelta(days=9), utcnow() + timedelta(days=9, hours=1)))
    await db.add(Coaching(user_id=host, skill_id="test", price=800))

    async def book(subject: str) -> str:
        offered = await learning.coaching_offer("test", UUID(slot.id), _user(subject))
        data = booking_contracts.Acceptance(
            order_id=offered["offer"]["id"],
            offer_hash=offered["offer"]["hash"],
            accepted=True,
            early_performance_requested=True,
        )
        await learning.book_coaching(data, "test", UUID(slot.id), _user(subject))
        return str(data.order_id)

    if first_outcome == "rejected":
        spend_coins.return_value = "failed"
        with pytest.raises(NotEnoughCoinsError):
            await book(STUDENT)
        first = (await db.all(select(BookingPayment)))[0].id
        assert (required(await db.get(BookingPayment, id=first))).state == "failed"
        spend_coins.return_value = "paid"
    else:
        first = await book(STUDENT)
        prepared = await ordinary_cancellations.prepare(
            _user(STUDENT), slot.id, CancellationPreparation(kind="coaching")
        )
        result = await ordinary_cancellations.receive(
            _user(STUDENT),
            str(uuid4()),
            CancellationDeclaration(
                target_id=prepared["id"],
                cancel_selected_scope=True,
                original_text="Cancel this selected coaching booking.",
            ),
        )
        assert result["state"] == "applied"
        await db.commit()
        assert (required(await db.get(BookingContract, id=first))).closed is True
    assert slot.booked_by is None and slot.payment_id is None
    current = await book(second)
    assert current != first
    from copy import deepcopy

    def snapshot(row: Any) -> dict[str, Any]:
        return deepcopy({column.name: getattr(row, column.name) for column in row.__table__.columns})

    before = snapshot(await db.get(BookingPayment, id=current))
    contract_before = snapshot(await db.get(BookingContract, id=current))
    slot_before = snapshot(slot)
    claims_before = [snapshot(claim) for claim in await db.all(select(SettlementClaim))]
    assert (
        required((required(await db.get(EventSubjectGuard, subject=STUDENT))).booking_reservations)[first]["event_id"]
        == slot.id
    )
    await delete_user_data(STUDENT)
    await db.commit()
    assert await db.all(select(RetainedEventRight)) == []
    assert (await export_user_data(STUDENT)).retained_event_rights["rights"] == []
    assert snapshot(await db.get(BookingPayment, id=current)) == before
    assert snapshot(await db.get(BookingContract, id=current)) == contract_before
    assert snapshot(slot) == slot_before
    claims_after = [snapshot(claim) for claim in await db.all(select(SettlementClaim))]
    assert all(claim in claims_after for claim in claims_before)
    # Erasure may preserve the old failed order's own unresolved evidence. It
    # must not create a claim over the replacement student's separate payment.
    assert all(claim["user_id"] != second and current not in str(claim) for claim in claims_after)
    await delete_user_data(STUDENT)
    assert await db.all(select(RetainedEventRight)) == []
    await delete_user_data(second)
    right = (await db.all(select(RetainedEventRight)))[0]
    assert right.source_subject == second and right.payment_id == current
    assert slot.booked_by == second and slot.payment_id == current


async def test_scoped_new_participant_reads_keep_confirmed_booking_access(
    session: AsyncSession, mocker: MockerFixture, spend_coins: AsyncMock
) -> None:
    from datetime import timedelta
    from unittest.mock import AsyncMock

    from api.schemas.user import UserInfo
    from api.utils.utc import utcnow

    event = _webinar()
    event.id = str(uuid4())
    event.start = utcnow() + timedelta(hours=3)
    event.end = event.start + timedelta(hours=1)
    await db.add(event)
    mocker.patch(
        "api.endpoints.calendar.get_userinfo",
        AsyncMock(
            return_value=UserInfo(id=event.creator, name="synthetic", display_name="Synthetic host", avatar_url=None)
        ),
    )
    offered = await learning.webinar_offer(UUID(event.id), _user(STUDENT))
    data = booking_contracts.Acceptance(
        order_id=offered["offer"]["id"],
        offer_hash=offered["offer"]["hash"],
        accepted=True,
        early_performance_requested=True,
    )
    await learning.book_webinar(data, UUID(event.id), _user(STUDENT))
    calendar = await learning.calendar(_user(STUDENT))
    displayed = next(row for row in calendar["events"] if row.id == event.id)
    detail = await learning.webinar(UUID(event.id), _user(STUDENT))
    assert displayed.link == detail.link == event.link
    assert displayed.admin_link is None and detail.admin_link is None
    assert spend_coins.await_count == 1


clear_cache_patch = test_webinars.clear_cache_patch

contract_backend = test_webinars.contract_backend

send_email = test_webinars.send_email

serialize_patch = test_webinars.serialize_patch

spend_coins = test_webinars.spend_coins
