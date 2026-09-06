from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db
from api.endpoints.coachings import set_coaching
from api.exceptions.skills import SkillRequirementsNotMetError
from api.models import Coaching
from api.schemas.coachings import UpdateCoaching
from api.schemas.user import User
from api.settings import settings


USER = "40ab0e5c-b7ee-4a25-9d10-1eaf3c62d2bd"
SKILL = "test"


@pytest.fixture(autouse=True)
def clear_cache_patch(mocker: MockerFixture) -> AsyncMock:
    return mocker.patch("api.endpoints.coachings.clear_cache", AsyncMock())


def _skill_levels(mocker: MockerFixture, level: int) -> AsyncMock:
    return mocker.patch("api.endpoints.coachings.get_skill_levels", AsyncMock(return_value={SKILL: level}))


async def test__set_coaching__below_the_required_level(mocker: MockerFixture, session: AsyncSession) -> None:
    _skill_levels(mocker, settings.coaching_level - 1)

    with pytest.raises(SkillRequirementsNotMetError):
        await set_coaching(UpdateCoaching(price=42), SKILL, User(id=USER, email_verified=True, admin=False))

    assert await db.get(Coaching, user_id=USER, skill_id=SKILL) is None


async def test__set_coaching__at_the_required_level(mocker: MockerFixture, session: AsyncSession) -> None:
    _skill_levels(mocker, settings.coaching_level)

    result = await set_coaching(UpdateCoaching(price=42), SKILL, User(id=USER, email_verified=True, admin=False))

    assert result.skill_id == SKILL
    assert result.price == 42
    assert await db.get(Coaching, user_id=USER, skill_id=SKILL) is not None


async def test__set_coaching__without_the_skill(mocker: MockerFixture, session: AsyncSession) -> None:
    mocker.patch("api.endpoints.coachings.get_skill_levels", AsyncMock(return_value={}))

    with pytest.raises(SkillRequirementsNotMetError):
        await set_coaching(UpdateCoaching(price=42), SKILL, User(id=USER, email_verified=True, admin=False))


async def test__set_coaching__admin_does_not_need_the_level(mocker: MockerFixture, session: AsyncSession) -> None:
    get_skill_levels = _skill_levels(mocker, 0)

    result = await set_coaching(UpdateCoaching(price=42), SKILL, User(id=USER, email_verified=True, admin=True))

    assert result.price == 42
    get_skill_levels.assert_not_awaited()


async def test__set_coaching__updates_the_price(mocker: MockerFixture, session: AsyncSession) -> None:
    _skill_levels(mocker, settings.coaching_level)
    await db.add(Coaching(user_id=USER, skill_id=SKILL, price=42))

    result = await set_coaching(UpdateCoaching(price=1337), SKILL, User(id=USER, email_verified=True, admin=False))

    assert result.price == 1337
    coaching = await db.get(Coaching, user_id=USER, skill_id=SKILL)
    assert coaching is not None and coaching.price == 1337
