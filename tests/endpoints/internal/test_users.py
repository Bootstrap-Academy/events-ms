from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db, select
from api.models import Coaching
from api.utils.jwt import encode_jwt


USER = "40ab0e5c-b7ee-4a25-9d10-1eaf3c62d2bd"


@pytest.fixture(autouse=True)
def clear_cache_patch(mocker: MockerFixture) -> AsyncMock:
    return mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())


def _internal_token() -> str:
    return encode_jwt({"aud": "events"}, timedelta(seconds=10))


async def test__delete_user(client: AsyncClient, session: AsyncSession) -> None:
    await db.add(Coaching(user_id=USER, skill_id="test", price=42))
    await db.add(Coaching(user_id="other", skill_id="test", price=42))

    response = await client.delete(f"/_internal/users/{USER}", headers={"Authorization": _internal_token()})

    assert response.status_code == 204
    assert not response.content
    assert [c.user_id for c in await db.all(select(Coaching))] == ["other"]


async def test__delete_user__unknown_user(client: AsyncClient, session: AsyncSession) -> None:
    await db.add(Coaching(user_id="other", skill_id="test", price=42))

    response = await client.delete("/_internal/users/unknown", headers={"Authorization": _internal_token()})

    assert response.status_code == 204
    assert [c.user_id for c in await db.all(select(Coaching))] == ["other"]


async def test__delete_user__unauthorized(client: AsyncClient, session: AsyncSession) -> None:
    await db.add(Coaching(user_id=USER, skill_id="test", price=42))

    response = await client.delete(f"/_internal/users/{USER}")

    assert response.status_code == 401
    assert [c.user_id for c in await db.all(select(Coaching))] == [USER]


async def test__delete_user__wrong_audience(client: AsyncClient, session: AsyncSession) -> None:
    token = encode_jwt({"aud": "skills"}, timedelta(seconds=10))

    response = await client.delete(f"/_internal/users/{USER}", headers={"Authorization": token})

    assert response.status_code == 401
