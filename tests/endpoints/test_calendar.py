from unittest.mock import AsyncMock

from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from api.endpoints.calendar import download_ics, rotate_ics_token
from api.models import CalendarToken
from api.schemas.user import User


USER = "40ab0e5c-b7ee-4a25-9d10-1eaf3c62d2bd"


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
