from datetime import timedelta
from unittest.mock import AsyncMock, call

import pytest
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db, select
from api.endpoints.calendar import cancel_event, download_ics, rotate_ics_token
from api.exceptions.auth import PermissionDeniedError
from api.exceptions.slots import SlotNotFoundException
from api.models import CalendarToken, EmergencyCancel, EventType, Slot, Webinar, WebinarParticipant
from api.schemas.user import User, UserInfo
from api.settings import settings
from api.utils.utc import utcnow


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
    return WebinarParticipant(webinar_id="webinar", user_id=user_id, paid_coins=paid_coins)


def _slot(start: timedelta, booked_by: str | None = STUDENT) -> Slot:
    return Slot(
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
def clear_cache_patch(mocker: MockerFixture) -> AsyncMock:
    return mocker.patch("api.endpoints.calendar.clear_cache", AsyncMock())


@pytest.fixture(autouse=True)
def add_coins(mocker: MockerFixture) -> AsyncMock:
    return mocker.patch("api.services.shop.add_coins", AsyncMock(return_value=True))


@pytest.fixture(autouse=True)
def spend_coins(mocker: MockerFixture) -> AsyncMock:
    return mocker.patch("api.services.shop.spend_coins", AsyncMock(return_value=True))


@pytest.fixture(autouse=True)
def send_email(mocker: MockerFixture) -> AsyncMock:
    """Replace the lowest level of the mail path, so the templates are still rendered."""

    return mocker.patch("api.utils.email.send_email", AsyncMock())


@pytest.fixture(autouse=True)
def userinfo(mocker: MockerFixture) -> AsyncMock:
    mocker.patch("api.utils.email.get_email", AsyncMock(side_effect=lambda user_id: f"{user_id}@example.com"))
    return mocker.patch(
        "api.endpoints.calendar.get_userinfo",
        AsyncMock(return_value=UserInfo(id=LECTURER, name="lecturer", display_name="Lecturer Person", avatar_url=None)),
    )


def _recipients(send_email: AsyncMock) -> list[tuple[str, str]]:
    return [(c.args[0], c.args[1]) for c in send_email.await_args_list]


def _body(send_email: AsyncMock, recipient: str) -> str:
    return next(" ".join(c.args[2].split()) for c in send_email.await_args_list if c.args[0] == recipient)


async def test__cancel_event__unknown_event(session: AsyncSession) -> None:
    with pytest.raises(SlotNotFoundException):
        await cancel_event("does not exist", _user(USER))


async def test__cancel_event__webinar_of_somebody_else(session: AsyncSession, add_coins: AsyncMock) -> None:
    await db.add(_webinar(timedelta(days=14)))

    with pytest.raises(SlotNotFoundException):
        await cancel_event("webinar", _user(OTHER))

    assert await db.get(Webinar, id="webinar") is not None
    add_coins.assert_not_awaited()


async def test__cancel_event__webinar_that_has_already_started(session: AsyncSession, add_coins: AsyncMock) -> None:
    await db.add(_webinar(timedelta(hours=-1)))

    with pytest.raises(PermissionDeniedError):
        await cancel_event("webinar", _user(LECTURER))

    assert await db.get(Webinar, id="webinar") is not None
    add_coins.assert_not_awaited()


@pytest.mark.parametrize("actor", [LECTURER, ADMIN])
async def test__cancel_event__webinar_cancelled__refunds_every_participant_exactly_once(
    session: AsyncSession, add_coins: AsyncMock, spend_coins: AsyncMock, actor: str
) -> None:
    await db.add(_webinar(timedelta(days=2)))
    await db.add(_participant(STUDENT))
    await db.add(_participant(OTHER))

    assert await cancel_event("webinar", _user(actor, admin=actor == ADMIN)) is True

    # the participants are not loaded in a defined order, so the refunds are compared as a set
    assert sorted(c.args for c in add_coins.await_args_list) == sorted(
        [
            (STUDENT, PRICE, "Cancel webinar 'test webinar'", False),
            (OTHER, PRICE, "Cancel webinar 'test webinar'", False),
        ]
    )
    spend_coins.assert_not_awaited()
    assert await db.get(Webinar, id="webinar") is None
    assert await db.all(select(WebinarParticipant)) == []


async def test__cancel_event__free_webinar_cancelled__no_refund(
    session: AsyncSession, add_coins: AsyncMock, spend_coins: AsyncMock
) -> None:
    await db.add(_webinar(timedelta(days=2), price=0))
    await db.add(_participant(STUDENT, 0))

    assert await cancel_event("webinar", _user(LECTURER)) is True

    add_coins.assert_not_awaited()
    spend_coins.assert_not_awaited()


async def test__cancel_event__webinar_cancelled__refunds_what_each_participant_paid(
    session: AsyncSession, add_coins: AsyncMock
) -> None:
    """A registration that was free must not be refunded, and a changed price must not change a refund."""

    await db.add(_webinar(timedelta(days=2)))
    await db.add(_participant(STUDENT, PRICE // 4))
    await db.add(_participant(OTHER, 0))

    assert await cancel_event("webinar", _user(LECTURER)) is True

    assert add_coins.await_args_list == [call(STUDENT, PRICE // 4, "Cancel webinar 'test webinar'", False)]


async def test__cancel_event__free_registrations_cancelled__refunds_nothing(
    session: AsyncSession, add_coins: AsyncMock
) -> None:
    """
    The loop of the emergency cancellation.

    A lecturer who cancels a webinar with participants owes the next booking, which is then free. Cancelling that
    webinar as well used to credit its price to a participant who had paid nothing for it, so lecturer and
    participant could create coins by repeating the two steps.
    """

    await db.add(_webinar(timedelta(days=2)))
    await db.add(_participant(STUDENT, 0))

    assert await cancel_event("webinar", _user(LECTURER)) is True

    add_coins.assert_not_awaited()
    assert await EmergencyCancel.exists(LECTURER) is True


async def test__cancel_event__webinar_cancelled__mails_the_participants_and_the_lecturer(
    session: AsyncSession, send_email: AsyncMock
) -> None:
    await db.add(_webinar(timedelta(days=2)))
    await db.add(_participant(STUDENT))
    await db.add(_participant(OTHER))

    await cancel_event("webinar", _user(LECTURER))

    recipients = _recipients(send_email)
    assert sorted(recipients[:2]) == sorted(
        [
            (f"{STUDENT}@example.com", "Stornierung deiner Buchung - Bootstrap Academy"),
            (f"{OTHER}@example.com", "Stornierung deiner Buchung - Bootstrap Academy"),
        ]
    )
    assert recipients[2:] == [(f"{LECTURER}@example.com", "Stornierung eines Termins - Bootstrap Academy")]
    body = _body(send_email, f"{STUDENT}@example.com")
    assert 'Das Webinar "test webinar" am' in body
    assert "wurde abgesagt" in body
    assert f"Wir haben dir {PRICE} MorphCoins zurückerstattet." in body
    assert 'Dein Webinar "test webinar" am' in _body(send_email, f"{LECTURER}@example.com")


async def test__cancel_event__webinar_with_participants_cancelled__owes_the_next_event(session: AsyncSession) -> None:
    await db.add(_webinar(timedelta(days=2)))
    await db.add(_participant(STUDENT))

    await cancel_event("webinar", _user(LECTURER))

    assert await EmergencyCancel.exists(LECTURER) is True


async def test__cancel_event__empty_webinar_cancelled__owes_nothing(session: AsyncSession) -> None:
    await db.add(_webinar(timedelta(days=2)))

    await cancel_event("webinar", _user(LECTURER))

    assert await EmergencyCancel.exists(LECTURER) is False


async def test__cancel_event__registration_cancelled_a_week_ahead__full_refund(
    session: AsyncSession, add_coins: AsyncMock
) -> None:
    await db.add(_webinar(timedelta(days=8)))
    await db.add(_participant(STUDENT))

    assert await cancel_event("webinar", _user(STUDENT)) is True

    assert add_coins.await_args_list == [call(STUDENT, PRICE, "Cancel webinar 'test webinar'", False)]
    assert await db.all(select(WebinarParticipant)) == []
    assert await db.get(Webinar, id="webinar") is not None


async def test__cancel_event__registration_cancelled_a_day_ahead__half_refund(
    session: AsyncSession, add_coins: AsyncMock
) -> None:
    await db.add(_webinar(timedelta(days=2)))
    await db.add(_participant(STUDENT))

    assert await cancel_event("webinar", _user(STUDENT)) is True

    assert add_coins.await_args_list == [
        call(STUDENT, PRICE // 2, "Cancel webinar 'test webinar'", False),
        call(LECTURER, int(PRICE * (1 - settings.event_fee) // 2), "Cancel webinar 'test webinar'", False),
    ]


async def test__cancel_event__registration_cancelled_a_week_ahead__refunds_what_was_paid(
    session: AsyncSession, add_coins: AsyncMock
) -> None:
    await db.add(_webinar(timedelta(days=8)))
    await db.add(_participant(STUDENT, PRICE // 4))

    assert await cancel_event("webinar", _user(STUDENT)) is True

    assert add_coins.await_args_list == [call(STUDENT, PRICE // 4, "Cancel webinar 'test webinar'", False)]


async def test__cancel_event__registration_cancelled_a_day_ahead__halves_what_was_paid(
    session: AsyncSession, add_coins: AsyncMock
) -> None:
    """The tiers apply to the amount that was paid, so the lecturer's share is half of half of that amount."""

    paid = PRICE // 4
    await db.add(_webinar(timedelta(days=2)))
    await db.add(_participant(STUDENT, paid))

    assert await cancel_event("webinar", _user(STUDENT)) is True

    assert add_coins.await_args_list == [
        call(STUDENT, paid // 2, "Cancel webinar 'test webinar'", False),
        call(LECTURER, int(paid * (1 - settings.event_fee) // 2), "Cancel webinar 'test webinar'", False),
    ]


async def test__cancel_event__free_registration_cancelled__pays_out_nothing(
    session: AsyncSession, add_coins: AsyncMock
) -> None:
    await db.add(_webinar(timedelta(days=2)))
    await db.add(_participant(STUDENT, 0))

    assert await cancel_event("webinar", _user(STUDENT)) is True

    add_coins.assert_not_awaited()
    assert await db.all(select(WebinarParticipant)) == []


async def test__cancel_event__registration_cancelled_within_a_day__forbidden(
    session: AsyncSession, add_coins: AsyncMock, send_email: AsyncMock
) -> None:
    await db.add(_webinar(timedelta(hours=12)))
    await db.add(_participant(STUDENT))

    with pytest.raises(PermissionDeniedError):
        await cancel_event("webinar", _user(STUDENT))

    add_coins.assert_not_awaited()
    send_email.assert_not_awaited()
    assert len(await db.all(select(WebinarParticipant))) == 1


async def test__cancel_event__registration_cancelled__mails_both_sides(
    session: AsyncSession, send_email: AsyncMock
) -> None:
    await db.add(_webinar(timedelta(days=8)))
    await db.add(_participant(STUDENT))

    await cancel_event("webinar", _user(STUDENT))

    assert _recipients(send_email) == [
        (f"{STUDENT}@example.com", "Stornierung deiner Buchung - Bootstrap Academy"),
        (f"{LECTURER}@example.com", "Stornierung eines Termins - Bootstrap Academy"),
    ]
    assert 'Deine Anmeldung für das Webinar "test webinar" am' in _body(send_email, f"{STUDENT}@example.com")
    assert "Eine Anmeldung für dein Webinar" in _body(send_email, f"{LECTURER}@example.com")


async def test__cancel_event__coaching_of_somebody_else(session: AsyncSession, add_coins: AsyncMock) -> None:
    await db.add(_slot(timedelta(days=14)))

    with pytest.raises(SlotNotFoundException):
        await cancel_event("slot", _user(OTHER))

    slot = await db.get(Slot, id="slot")
    assert slot is not None and slot.booked_by == STUDENT
    add_coins.assert_not_awaited()


async def test__cancel_event__free_coaching_slot(session: AsyncSession) -> None:
    await db.add(_slot(timedelta(days=14), booked_by=None))

    with pytest.raises(SlotNotFoundException):
        await cancel_event("slot", _user(LECTURER))


async def test__cancel_event__coaching_that_has_already_started(session: AsyncSession, add_coins: AsyncMock) -> None:
    await db.add(_slot(timedelta(hours=-1)))

    with pytest.raises(PermissionDeniedError):
        await cancel_event("slot", _user(LECTURER))

    add_coins.assert_not_awaited()


async def test__cancel_event__lecturer_cancels_coaching__refunds_the_student(
    session: AsyncSession, add_coins: AsyncMock
) -> None:
    await db.add(_slot(timedelta(days=2)))

    assert await cancel_event("slot", _user(LECTURER)) is True

    assert add_coins.await_args_list == [call(STUDENT, STUDENT_COINS, "Cancel coaching", False)]
    assert await EmergencyCancel.exists(LECTURER) is True


async def test__cancel_event__admin_cancels_coaching__refunds_the_student(
    session: AsyncSession, add_coins: AsyncMock
) -> None:
    """An admin who is neither the lecturer nor the student used to free the slot without refunding anybody."""

    await db.add(_slot(timedelta(days=2)))

    assert await cancel_event("slot", _user(ADMIN, admin=True)) is True

    assert add_coins.await_args_list == [call(STUDENT, STUDENT_COINS, "Cancel coaching", False)]
    # the lecturer did not cancel, so they do not owe a free event
    assert await EmergencyCancel.exists(LECTURER) is False


async def test__cancel_event__student_cancels_coaching_a_week_ahead__full_refund(
    session: AsyncSession, add_coins: AsyncMock
) -> None:
    await db.add(_slot(timedelta(days=8)))

    assert await cancel_event("slot", _user(STUDENT)) is True

    assert add_coins.await_args_list == [call(STUDENT, STUDENT_COINS, "Cancel coaching", False)]
    assert await EmergencyCancel.exists(LECTURER) is False


async def test__cancel_event__student_cancels_coaching_a_day_ahead__half_refund(
    session: AsyncSession, add_coins: AsyncMock
) -> None:
    await db.add(_slot(timedelta(days=2)))

    assert await cancel_event("slot", _user(STUDENT)) is True

    assert add_coins.await_args_list == [
        call(STUDENT, STUDENT_COINS // 2, "Cancel coaching", False),
        call(LECTURER, INSTRUCTOR_COINS // 2, "Cancel coaching", False),
    ]


async def test__cancel_event__student_cancels_coaching_within_a_day__forbidden(
    session: AsyncSession, add_coins: AsyncMock, send_email: AsyncMock
) -> None:
    await db.add(_slot(timedelta(hours=12)))

    with pytest.raises(PermissionDeniedError):
        await cancel_event("slot", _user(STUDENT))

    add_coins.assert_not_awaited()
    send_email.assert_not_awaited()
    slot = await db.get(Slot, id="slot")
    assert slot is not None and slot.booked_by == STUDENT


async def test__cancel_event__cancelled_coaching__frees_the_slot(session: AsyncSession) -> None:
    await db.add(_slot(timedelta(days=2)))

    await cancel_event("slot", _user(ADMIN, admin=True))

    slot = await db.get(Slot, id="slot")
    assert slot is not None
    assert slot.booked_by is None
    assert slot.event_type is None
    assert slot.student_coins is None
    assert slot.instructor_coins is None
    assert slot.link is None


async def test__cancel_event__cancelled_coaching__mails_both_sides(
    session: AsyncSession, send_email: AsyncMock
) -> None:
    await db.add(_slot(timedelta(days=2)))

    await cancel_event("slot", _user(LECTURER))

    assert _recipients(send_email) == [
        (f"{STUDENT}@example.com", "Stornierung deiner Buchung - Bootstrap Academy"),
        (f"{LECTURER}@example.com", "Stornierung eines Termins - Bootstrap Academy"),
    ]
    student_body = _body(send_email, f"{STUDENT}@example.com")
    assert "Dein Coaching mit Lecturer Person am" in student_body
    assert "wurde abgesagt" in student_body
    assert f"Wir haben dir {STUDENT_COINS} MorphCoins zurückerstattet." in student_body
    assert "Der Termin ist wieder buchbar." in _body(send_email, f"{LECTURER}@example.com")


async def test__cancel_event__student_cancels_coaching__mails_both_sides(
    session: AsyncSession, send_email: AsyncMock
) -> None:
    await db.add(_slot(timedelta(days=2)))

    await cancel_event("slot", _user(STUDENT))

    student_body = _body(send_email, f"{STUDENT}@example.com")
    assert "Deine Buchung des Coachings mit Lecturer Person am" in student_body
    assert f"Wir haben dir {STUDENT_COINS // 2} MorphCoins zurückerstattet." in student_body
    lecturer_body = _body(send_email, f"{LECTURER}@example.com")
    assert "Die Buchung deines Coaching-Termins am" in lecturer_body
    assert f"Dir wurden {INSTRUCTOR_COINS // 2} MorphCoins als Ausgleich gutgeschrieben." in lecturer_body


async def test__cancel_event__unknown_mail_address__cancels_anyway(
    session: AsyncSession, mocker: MockerFixture, add_coins: AsyncMock, send_email: AsyncMock
) -> None:
    mocker.patch("api.utils.email.get_email", AsyncMock(return_value=None))
    await db.add(_slot(timedelta(days=2)))

    assert await cancel_event("slot", _user(LECTURER)) is True

    send_email.assert_not_awaited()
    assert add_coins.await_args_list == [call(STUDENT, STUDENT_COINS, "Cancel coaching", False)]


async def test__cancel_event__failing_mail__cancels_anyway(
    session: AsyncSession, add_coins: AsyncMock, send_email: AsyncMock
) -> None:
    send_email.side_effect = ValueError("Invalid email address")
    await db.add(_slot(timedelta(days=2)))

    assert await cancel_event("slot", _user(LECTURER)) is True

    slot = await db.get(Slot, id="slot")
    assert slot is not None and slot.booked_by is None
    assert add_coins.await_args_list == [call(STUDENT, STUDENT_COINS, "Cancel coaching", False)]


async def test__cancel_event__clears_the_calendar_cache(session: AsyncSession, clear_cache_patch: AsyncMock) -> None:
    await db.add(_slot(timedelta(days=2)))

    await cancel_event("slot", _user(LECTURER))

    clear_cache_patch.assert_awaited_once_with("calendar")
