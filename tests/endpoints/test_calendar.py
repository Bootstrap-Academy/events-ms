import json
from datetime import timedelta
from typing import Any, AsyncIterator, Awaitable, Callable, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db, select
from api.endpoints.calendar import cancel_event, download_ics, rotate_ics_token
from api.models import (
    CalendarToken,
    CoinOperation,
    EmergencyCancel,
    EventType,
    SettlementClaim,
    Slot,
    Webinar,
    WebinarParticipant,
)
from api.models.ordinary_cancellation import OrdinaryEventCancellation
from api.schemas.ordinary_cancellation import CancellationDeclaration, CancellationPreparation
from api.schemas.user import User
from api.services import ordinary_cancellations as ordinary
from api.services import settlements
from api.utils.utc import utcnow
from tests.payment_fixtures import paid_participant, paid_slot
from tests.required import required


USER = "40ab0e5c-b7ee-4a25-9d10-1eaf3c62d2bd"
LECTURER = "9f4e2d17-9e2b-4b02-8c0f-3a8c07c5f4f0"
STUDENT = "c1d2eb59-8b1a-4a2f-9c37-1b8e5d7f60a3"
OTHER = "b5a6b0c2-0f39-4a3c-9a3a-2d8d3d9d4a11"
ADMIN = "6b2f8f3a-4f6a-4a3d-9a37-9f5f2a6c7c88"

PRICE = 1000
STUDENT_COINS = 800
INSTRUCTOR_COINS = 560


async def test__rotate_ics_token__revokes_the_previous_token(session: AsyncSession) -> None:
    old = (await CalendarToken.get_or_create(USER)).token

    result = await rotate_ics_token(User(id=USER, email_verified=True, admin=False))

    assert result.ics_token != old
    assert await CalendarToken.get_user_id(result.ics_token) == USER
    assert await CalendarToken.get_user_id(old) is None


async def test__download_ics__resolves_the_token(mocker: MockerFixture, session: AsyncSession) -> None:
    token = (await CalendarToken.get_or_create(USER)).token
    mocker.patch("api.endpoints.calendar.is_admin", AsyncMock(return_value=False))
    get_events = mocker.patch("api.endpoints.calendar.get_events", AsyncMock(return_value=[]))
    mocker.patch("api.endpoints.calendar.create_ics", AsyncMock(return_value=b"BEGIN:VCALENDAR"))

    response = await download_ics(None, None, None, None, token)

    assert response.status_code == 200
    assert response.media_type == "text/calendar"
    assert get_events.call_args.args[0] == USER


async def test__download_ics__unknown_token(mocker: MockerFixture, session: AsyncSession) -> None:
    await CalendarToken.get_or_create(USER)
    get_events = mocker.patch("api.endpoints.calendar.get_events", AsyncMock(return_value=[]))

    response = await download_ics(None, None, None, None, "some other token")

    assert response.status_code == 401
    get_events.assert_not_called()


def _user(user_id: str, admin: bool = False) -> User:
    return User(id=user_id, email_verified=True, admin=admin)


def _webinar(start: timedelta, price: int = PRICE) -> Webinar:
    return Webinar(
        id="webinar",
        skill_id="test",
        creator=LECTURER,
        creation_date=utcnow(),
        name="test webinar",
        description="test description",
        admin_link="https://meet.jit.si/admin",
        link="https://meet.jit.si/link",
        start=utcnow() + start,
        end=utcnow() + start + timedelta(hours=1),
        max_participants=42,
        price=price,
    )


def _participant(user_id: str, paid_coins: int = PRICE) -> WebinarParticipant:
    return paid_participant(webinar_id="webinar", user_id=user_id, paid_coins=paid_coins)


def _slot(start: timedelta, booked_by: str | None = STUDENT) -> Slot:
    return paid_slot(
        id="slot",
        user_id=LECTURER,
        start=utcnow() + start,
        end=utcnow() + start + timedelta(hours=1),
        booked_by=booked_by,
        event_type=EventType.COACHING if booked_by else None,
        student_coins=STUDENT_COINS if booked_by else None,
        instructor_coins=INSTRUCTOR_COINS if booked_by else None,
        skill_id="test" if booked_by else None,
        admin_link="https://meet.jit.si/admin" if booked_by else None,
        link="https://meet.jit.si/link" if booked_by else None,
    )


@pytest.fixture(autouse=True)
def local_effects(mocker: MockerFixture) -> AsyncMock:
    mocker.patch("api.services.ordinary_cancellations.clear_cache", AsyncMock())
    return mocker.patch("api.services.settlements.finish", AsyncMock())


def declaration(target: Any) -> dict[str, Any]:
    return {
        "target_id": target["id"],
        "cancel_selected_scope": True,
        "original_text": "I cancel exactly this displayed booking.",
    }


@pytest.mark.parametrize("denial", [None, "unauthorized", "invalid_schema"])
async def test_route_records_completed_declaration_body_before_slow_current_auth_and_only_persists_valid_intake(
    session: AsyncSession, client: AsyncClient, mocker: MockerFixture, denial: str
) -> None:
    webinar = await db.add(_webinar(timedelta(days=8)))
    await db.add(_participant(STUDENT, paid_coins=42))
    received = utcnow().replace(microsecond=654321)
    webinar.start = received + timedelta(days=7)
    webinar.end = webinar.start + timedelta(hours=1)
    await db.commit()
    prepared = await ordinary.prepare(_user(STUDENT), webinar.id, CancellationPreparation(kind="webinar"))
    clock = {"now": received - timedelta(seconds=1)}
    mocker.patch("api.endpoints.calendar.utcnow", side_effect=lambda: clock["now"])
    mocker.patch.object(ordinary, "utcnow", side_effect=lambda: clock["now"])
    mocker.patch(
        "api.auth.decode_jwt",
        return_value={"uid": STUDENT, "rt": "local-session", "data": {"admin": False, "email_verified": True}},
    )
    mocker.patch("api.schemas.user.UserAccessToken.is_revoked", AsyncMock(return_value=False))

    async def authority(access_token: str, expected_user_id: str) -> Any:
        assert clock["now"] == received, "timestamp is observed after complete body arrival"
        clock["now"] += timedelta(seconds=10)
        return None if denial == "unauthorized" else _user(STUDENT)

    mocker.patch("api.auth.ordinary_authority", side_effect=authority)
    command = str(uuid4())
    body = declaration(prepared)
    if denial == "invalid_schema":
        body["client_received_at"] = received.isoformat()
    encoded = json.dumps(body).encode()

    async def chunks() -> AsyncIterator[bytes]:
        yield encoded[:10]
        clock["now"] = received
        yield encoded[10:]

    response = await client.post(
        f"/calendar/cancellations/{command}",
        content=chunks(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer local"},
    )
    if denial:
        assert response.status_code in (401, 422)
        assert await db.get(OrdinaryEventCancellation, id=command) is None
        assert await db.all(select(SettlementClaim)) == []
        assert await db.get(WebinarParticipant, webinar_id=webinar.id, user_id=STUDENT) is not None
    else:
        assert response.status_code == 200, response.text
        assert response.json()["received_at"] == received.isoformat()
        claim: SettlementClaim = required(
            cast(
                SettlementClaim | None,
                await db.first(select(SettlementClaim).where(SettlementClaim.user_id == STUDENT)),
            )
        )
        assert claim.coins == 42 and claim.entitlement == "established"
        assert response.json()["financial_satisfaction"] is False


async def test_old_event_only_public_and_internal_callers_fail_without_creating_a_declaration(
    session: AsyncSession,
) -> None:
    from api.endpoints.internal.users import cancel_recipient_event

    event = await db.add(_webinar(timedelta(days=9)))
    await db.add(_participant(STUDENT))
    await db.commit()
    operations: list[Callable[[], Awaitable[None]]] = [
        lambda: cancel_event(event.id, _user(STUDENT)),
        lambda: cancel_recipient_event(STUDENT, event.id),
    ]
    for operation in operations:
        with pytest.raises(HTTPException) as exc:
            await operation()
        assert exc.value.status_code == 409 and cast(dict[str, Any], exc.value.detail)["cancellation_recorded"] is False
    assert len(await db.all(select(WebinarParticipant))) == 1
    assert await db.all(select(OrdinaryEventCancellation)) == []


@pytest.mark.parametrize("paid", [0, 42, 1337])
@pytest.mark.parametrize("days", [9, 1, -1])
async def test_student_cancellation_uses_original_paid_amount_and_reviews_late_claims_without_half_or_forfeiture(
    session: AsyncSession, paid: int, days: int
) -> None:
    event = await db.add(_webinar(timedelta(days=days), price=9999))
    await db.add(_participant(STUDENT, paid_coins=paid))
    await db.commit()
    prepared = await ordinary.prepare(_user(STUDENT), event.id, CancellationPreparation(kind="webinar"))
    command, body = str(uuid4()), CancellationDeclaration(**declaration(prepared))
    result = await ordinary.receive(_user(STUDENT), command, body)
    assert result["state"] == "applied"
    claim: SettlementClaim = required(
        cast(SettlementClaim | None, await db.first(select(SettlementClaim).where(SettlementClaim.user_id == STUDENT)))
    )
    assert claim.coins == paid and claim.entitlement == ("established" if days == 9 else "pending_evidence")
    assert await db.get(WebinarParticipant, webinar_id=event.id, user_id=STUDENT) is None
    assert await db.get(Webinar, id=event.id) is not None
    assert await ordinary.receive(_user(STUDENT), command, body) == result
    assert len([row for row in await db.all(select(CoinOperation)) if row.user_id == STUDENT]) == (1 if paid else 0)


@pytest.mark.parametrize("with_students", [False, True])
async def test_provider_whole_session_cancels_exact_original_orders_and_empty_session_has_no_waiver(
    session: AsyncSession, with_students: bool
) -> None:
    event = await db.add(_webinar(timedelta(days=2)))
    if with_students:
        await db.add(_participant(STUDENT, paid_coins=42))
        await db.add(_participant(OTHER, paid_coins=0))
    await db.commit()
    prepared = await ordinary.prepare(
        _user(LECTURER), event.id, CancellationPreparation(kind="webinar", scope="session")
    )
    result = await ordinary.receive(_user(LECTURER), str(uuid4()), CancellationDeclaration(**declaration(prepared)))
    assert result["state"] == "applied" and await db.get(Webinar, id=event.id) is None
    claims = {row.user_id: row.coins for row in await db.all(select(SettlementClaim))}
    assert claims == ({STUDENT: 42, OTHER: 0} if with_students else {})
    assert await EmergencyCancel.exists(LECTURER) is with_students


@pytest.mark.parametrize("contact", ["accepted", "missing", "unverified", "smtp_failure"])
async def test_active_generic_notice_and_handoff_keep_original_origin_and_financial_uncertainty(
    session: AsyncSession, mocker: MockerFixture, local_effects: AsyncMock, contact: str
) -> None:
    from api.services.internal import InternalService
    from api.utils import email

    event = await db.add(_webinar(timedelta(days=9)))
    await db.add(_participant(STUDENT, paid_coins=42))
    await db.commit()
    prepared = await ordinary.prepare(_user(STUDENT), event.id, CancellationPreparation(kind="webinar"))
    command = str(uuid4())
    result = await ordinary.receive(_user(STUDENT), command, CancellationDeclaration(**declaration(prepared)))
    assert result["state"] == "applied"
    receipt = required(await db.get(OrdinaryEventCancellation, id=command))
    sent = mocker.patch.object(
        email,
        "send_email",
        AsyncMock(side_effect=RuntimeError("SMTP unavailable") if contact == "smtp_failure" else None),
    )
    requests = []

    def response(request: Any) -> Any:
        requests.append(str(request.url))
        if contact == "missing":
            return httpx.Response(404, json={})
        return httpx.Response(
            200, json={"email": "synthetic@example.invalid", "email_verified": contact != "unverified"}
        )

    transport = httpx.MockTransport(response)
    # Installed normal HTTP client fixture only; no sockets or service lifecycle.
    mocker.patch.object(InternalService, "_get_token", return_value="synthetic-internal-fixture")
    mocker.patch(
        "api.services.internal.AsyncClient",
        side_effect=lambda *a, **kw: httpx.AsyncClient(transport=transport, base_url="https://synthetic.invalid"),
    )
    handoffs = []

    async def commercial(operation: str, payload: dict[str, Any]) -> Any:
        assert operation == "register_event"
        handoffs.append(payload)
        return {
            "protocol": 1,
            "obligation_id": payload["obligation_id"],
            "disposition": "claim_preserved",
            "financial_satisfaction": False,
        }

    mocker.patch("api.services.shop.commercial", side_effect=commercial)
    await settlements.deliver([required(receipt.result)["batch_id"]])
    view = await ordinary.status(_user(STUDENT), command)
    assert view["financial_state"] == "pending" and view["financial_satisfaction"] is False
    assert view["notice_state"] == ("smtp_accepted" if contact == "accepted" else "pending")
    assert len(handoffs) == 1
    fact = handoffs[0]["observation"]["cancellation_evidence"][0]
    assert fact["origin"] == "ordinary_authenticated" and fact["command_id"] == command
    assert handoffs[0]["operation_id"] == handoffs[0]["obligation_id"]
    assert requests
    if contact == "accepted":
        assert len(sent.await_args_list) == 2
        html = " ".join(sent.await_args_list[0].args[2].split())
        assert command in html and "test webinar" in html and "keine Zahlung und keine Gutschrift" in html
        assert "50% Rückerstattung" not in html and "wieder gutgeschrieben" not in html
        await settlements.deliver([required(receipt.result)["batch_id"]])
        assert len(sent.await_args_list) == 2 and len(handoffs) == 1
