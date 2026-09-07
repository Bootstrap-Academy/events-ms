from datetime import timedelta
from unittest.mock import AsyncMock, call

import pytest
from pytest_mock import MockerFixture

from api.database import db, db_context, select
from api.models import EmergencyCancel, Webinar, WebinarParticipant
from api.models.webinars import clean_old_webinars
from api.settings import settings
from api.utils.utc import utcnow


LECTURER = "9f4e2d17-9e2b-4b02-8c0f-3a8c07c5f4f0"
STUDENT = "c1d2eb59-8b1a-4a2f-9c37-1b8e5d7f60a3"
OTHER = "b5a6b0c2-0f39-4a3c-9a3a-2d8d3d9d4a11"

PRICE = 1000


@pytest.fixture(autouse=True)
def add_coins(mocker: MockerFixture) -> AsyncMock:
    return mocker.patch("api.services.shop.add_coins", AsyncMock(return_value=True))


@pytest.fixture(autouse=True)
def add_xp(mocker: MockerFixture) -> AsyncMock:
    return mocker.patch("api.models.webinars.add_xp", AsyncMock())


async def _past_webinar(participants: list[tuple[str, int]]) -> None:
    async with db_context():
        await db.add(
            Webinar(
                id="webinar",
                skill_id="test",
                creator=LECTURER,
                creation_date=utcnow(),
                name="test webinar",
                description="test description",
                admin_link="https://meet.jit.si/admin",
                link="https://meet.jit.si/link",
                start=utcnow() - timedelta(hours=2),
                end=utcnow() - timedelta(hours=1),
                max_participants=42,
                price=PRICE,
            )
        )
        for user_id, paid_coins in participants:
            await db.add(WebinarParticipant(webinar_id="webinar", user_id=user_id, paid_coins=paid_coins))


async def test__clean_old_webinars__pays_the_lecturer_their_share_of_what_was_paid(
    database: None, add_coins: AsyncMock
) -> None:
    await _past_webinar([(STUDENT, PRICE), (OTHER, PRICE)])

    await clean_old_webinars()

    assert add_coins.await_args_list == [call(LECTURER, int(2 * PRICE * (1 - settings.event_fee)), "Webinar", True)]
    async with db_context():
        assert await db.all(select(Webinar)) == []


async def test__clean_old_webinars__free_registrations_pay_out_nothing(database: None, add_coins: AsyncMock) -> None:
    """A registration made free by an emergency cancellation must not earn the lecturer their share of a price."""

    await _past_webinar([(STUDENT, 0), (OTHER, PRICE)])

    await clean_old_webinars()

    assert add_coins.await_args_list == [call(LECTURER, int(PRICE * (1 - settings.event_fee)), "Webinar", True)]


async def test__clean_old_webinars__settles_the_emergency_cancellation(database: None) -> None:
    async with db_context():
        await EmergencyCancel.create(LECTURER)
    await _past_webinar([(STUDENT, PRICE)])

    await clean_old_webinars()

    async with db_context():
        assert await EmergencyCancel.exists(LECTURER) is False
