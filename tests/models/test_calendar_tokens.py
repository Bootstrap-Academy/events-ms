from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db, select
from api.models import CalendarToken


USER = "40ab0e5c-b7ee-4a25-9d10-1eaf3c62d2bd"
OTHER = "9f4e2d17-9e2b-4b02-8c0f-3a8c07c5f4f0"


async def test__get_or_create__creates_exactly_one_token_per_user(session: AsyncSession) -> None:
    token = await CalendarToken.get_or_create(USER)

    assert token.user_id == USER
    assert len(token.token) >= 32
    assert await CalendarToken.get_or_create(USER) is token
    assert len(await db.all(select(CalendarToken))) == 1


async def test__get_or_create__gives_every_user_a_different_token(session: AsyncSession) -> None:
    assert (await CalendarToken.get_or_create(USER)).token != (await CalendarToken.get_or_create(OTHER)).token


async def test__rotate__replaces_the_previous_token(session: AsyncSession) -> None:
    old = (await CalendarToken.get_or_create(USER)).token

    new = (await CalendarToken.rotate(USER)).token

    assert new != old
    assert await CalendarToken.get_user_id(old) is None
    assert await CalendarToken.get_user_id(new) == USER
    assert len(await db.all(select(CalendarToken))) == 1


async def test__rotate__creates_a_token_for_a_user_without_one(session: AsyncSession) -> None:
    token = await CalendarToken.rotate(USER)

    assert await CalendarToken.get_user_id(token.token) == USER


async def test__get_user_id__unknown_token(session: AsyncSession) -> None:
    await CalendarToken.get_or_create(USER)

    assert await CalendarToken.get_user_id("not a token") is None
