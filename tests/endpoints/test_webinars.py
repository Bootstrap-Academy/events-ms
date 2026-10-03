from datetime import timedelta
from typing import Any
from unittest.mock import ANY, AsyncMock, call
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import AsyncClient, MockTransport, Request, Response
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db, select
from api.endpoints.webinars import register_for_webinar
from api.exceptions.coaching import NotEnoughCoinsError
from api.models import BookingContract, EmergencyCancel, Webinar, WebinarParticipant
from api.schemas.user import User, UserInfo
from api.services import booking_availability, booking_contracts
from api.services.internal import InternalService
from api.utils.utc import utcnow


LECTURER = "9f4e2d17-9e2b-4b02-8c0f-3a8c07c5f4f0"
STUDENT = "c1d2eb59-8b1a-4a2f-9c37-1b8e5d7f60a3"

PRICE = 1000


def _user(user_id: str) -> User:
    return User(id=user_id, email_verified=True, admin=False)


def _webinar(price: int = PRICE) -> Webinar:
    return Webinar(
        id="webinar",
        skill_id="test",
        creator=LECTURER,
        creation_date=utcnow(),
        name="test webinar",
        description="test description",
        admin_link="https://meet.jit.si/admin",
        link="https://meet.jit.si/link",
        start=utcnow() + timedelta(days=14),
        end=utcnow() + timedelta(days=14, hours=1),
        max_participants=42,
        price=price,
        participants=[],
    )


@pytest.fixture(autouse=True)
def clear_cache_patch(mocker: MockerFixture) -> AsyncMock:
    return mocker.patch("api.endpoints.webinars.clear_cache", AsyncMock())


@pytest.fixture(autouse=True)
def spend_coins(mocker: MockerFixture) -> AsyncMock:
    return AsyncMock(return_value="paid")


@pytest.fixture(autouse=True)
def contract_backend(mocker: MockerFixture, spend_coins: AsyncMock) -> dict[str, bool]:
    offers = {}
    control = {"confirmed": True}

    # These SQLite tests isolate reservation/financial bookkeeping using a
    # synthetic backend and source observation. Real committed availability,
    # deadlines and immutable witnesses run against migrated PostgreSQL/MySQL.
    mocker.patch.object(booking_availability, "supported", return_value=True)

    async def synthetic_observation(order_id: str) -> Any:
        contract = await db.get(BookingContract, id=order_id)
        candidate = contract.candidate if contract else None
        usable = bool(contract and not contract.closed and contract.state in ("candidate", "ready") and candidate)
        return usable, candidate, None, utcnow()

    mocker.patch.object(booking_availability, "read", synthetic_observation)

    async def handle(request: Request) -> Response:
        import json

        payload = json.loads(request.content) if request.content else {}
        if request.url.path.startswith("/purchase-offers/"):
            oid = str(uuid4())
            offer = {"id": oid, "hash": oid, "product": payload, "recipient": "synthetic@example.invalid"}
            offers[oid] = offer
            return Response(200, json={"offer": offer, "state": "offered"})
        if request.url.path.startswith("/purchase-fulfillment/"):
            return Response(200, json={})
        offer = offers[payload["order_id"]]
        price = offer["product"]["coins"]
        result = await spend_coins(offer["id"], STUDENT, price, "Webinar 'test webinar'") if price else "paid"
        return Response(
            200,
            json={
                "offer": offer,
                "state": "paid" if result == "paid" else "failed",
                "confirmation_smtp_accepted_at": utcnow().isoformat() if control["confirmed"] else None,
                "financial_evidence": {
                    "charged_coins": price,
                    "ledger_id": offer["id"] if price else None,
                    "no_charge": not bool(price),
                },
            },
        )

    mocker.patch.object(
        InternalService,
        "client",
        new_callable=property,
        fget=lambda _: AsyncClient(base_url="http://synthetic", transport=MockTransport(handle)),
    )
    mocker.patch(
        "api.services.booking_contracts.get_userinfo",
        AsyncMock(return_value=UserInfo(id=LECTURER, name="lecturer", display_name="Lecturer Person", avatar_url=None)),
    )
    return control


async def book(webinar: Webinar) -> Any:
    quote = await booking_contracts.offer(STUDENT, "webinar", webinar)
    data = booking_contracts.Acceptance(
        order_id=quote["offer"]["id"],
        offer_hash=quote["offer"]["hash"],
        accepted=True,
        early_performance_requested=True,
    )
    return await register_for_webinar(data, webinar, _user(STUDENT))


@pytest.fixture(autouse=True)
def serialize_patch(mocker: MockerFixture) -> None:
    """The response resolves the lecturer and their rating, both of which go through the cache."""

    mocker.patch(
        "api.models.webinars.get_userinfo",
        AsyncMock(return_value=UserInfo(id=LECTURER, name="lecturer", display_name="Lecturer Person", avatar_url=None)),
    )
    mocker.patch("api.models.webinars.LecturerRating.get_rating", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def send_email(mocker: MockerFixture) -> AsyncMock:
    """Replace the lowest level of the mail path, so the template is still rendered."""

    mocker.patch("api.utils.email.get_email", AsyncMock(side_effect=lambda user_id: f"{user_id}@example.com"))
    return mocker.patch("api.utils.email.send_email", AsyncMock())


async def _participants() -> list[WebinarParticipant]:
    return await db.all(select(WebinarParticipant))


async def test__register_for_webinar__records_what_was_paid(session: AsyncSession, spend_coins: AsyncMock) -> None:
    webinar = await db.add(_webinar())

    await book(webinar)

    assert spend_coins.await_args_list == [call(ANY, STUDENT, PRICE, "Webinar 'test webinar'")]
    participants = await _participants()
    assert [(p.user_id, p.paid_coins) for p in participants] == [(STUDENT, PRICE)]


async def test__register_for_webinar__emergency_cancel__is_free_and_records_nothing_paid(
    session: AsyncSession, spend_coins: AsyncMock
) -> None:
    """A lecturer who had to cancel owes the next booking, so it costs nothing and is refunded nothing later."""

    webinar = await db.add(_webinar())
    await EmergencyCancel.create(LECTURER)

    await book(webinar)

    spend_coins.assert_not_awaited()
    assert [(p.user_id, p.paid_coins) for p in await _participants()] == [(STUDENT, 0)]


async def test__register_for_webinar__emergency_cancel__is_consumed_by_the_booking(session: AsyncSession) -> None:
    """The debt is settled by the booking it makes free, instead of making every later booking free as well."""

    webinar = await db.add(_webinar())
    await EmergencyCancel.create(LECTURER)

    await book(webinar)

    assert await EmergencyCancel.exists(LECTURER) is False


async def test__register_for_webinar__not_enough_coins(session: AsyncSession, spend_coins: AsyncMock) -> None:
    spend_coins.return_value = "rejected"
    webinar = await db.add(_webinar())

    with pytest.raises(NotEnoughCoinsError):
        await book(webinar)

    assert await _participants() == []


async def test_confirmation_pending_keeps_booking_payment_without_join_access(
    session: AsyncSession, contract_backend: dict[str, bool]
) -> None:
    contract_backend["confirmed"] = False
    webinar = await db.add(_webinar())
    result = await book(webinar)
    assert result.booked is True
    assert result.link is None
    assert [(p.user_id, p.paid_coins) for p in await _participants()] == [(STUDENT, PRICE)]


async def test_wrong_offer_identity_has_no_financial_or_booking_effect(
    session: AsyncSession, spend_coins: AsyncMock
) -> None:
    webinar = await db.add(_webinar())
    data = booking_contracts.Acceptance(
        order_id=uuid4(), offer_hash="other-offer", accepted=True, early_performance_requested=True
    )
    with pytest.raises(HTTPException) as error:
        await register_for_webinar(data, webinar, _user(STUDENT))
    assert error.value.status_code == 404
    spend_coins.assert_not_awaited()
    assert await _participants() == []
