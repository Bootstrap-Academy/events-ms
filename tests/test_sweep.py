from datetime import time, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from _pytest.monkeypatch import MonkeyPatch
from pytest_mock import MockerFixture

from api.database import db, db_context, select
from api.models import Coaching, EmergencyCancel, Exam, LecturerRating, Slot, Webinar, WebinarParticipant, WeeklySlot
from api.services.internal import InternalServiceError
from api.settings import settings
from api.sweep import RateLimiter, main, sweep_deleted_users, user_id_batch_query
from api.utils.utc import utcnow


EXISTING = "11111111-1111-1111-1111-111111111111"
DELETED = "22222222-2222-2222-2222-222222222222"
UNKNOWN = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def clear_cache_patch(mocker: MockerFixture) -> AsyncMock:
    return mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deleted_user_sweep_rate_limit", 0)


@pytest.fixture
async def data(database: None) -> None:
    async with db_context():
        await db.add(
            Webinar(
                id="webinar",
                skill_id="test",
                creator=EXISTING,
                creation_date=utcnow(),
                name="test webinar",
                description="test description",
                admin_link="https://meet.jit.si/admin",
                link="https://meet.jit.si/link",
                start=utcnow() + timedelta(days=1),
                end=utcnow() + timedelta(days=1, hours=1),
                max_participants=42,
                price=1337,
            )
        )
        await db.add(WebinarParticipant(webinar_id="webinar", user_id=DELETED))
        await db.add(
            WeeklySlot(id="weekly", user_id=UNKNOWN, weekday=3, start=time(10, 0), end=time(11, 0), last_slot=utcnow())
        )
        await db.add(
            Slot(
                id="slot",
                user_id=EXISTING,
                start=utcnow() + timedelta(days=1),
                end=utcnow() + timedelta(days=1, hours=1),
                booked_by=DELETED,
                event_type=None,
                student_coins=None,
                instructor_coins=None,
                skill_id=None,
                admin_link=None,
                link=None,
                weekly_slot_id=None,
            )
        )
        await db.add(Coaching(user_id=DELETED, skill_id="test", price=42))
        await db.add(Exam(user_id=DELETED, skill_id="test"))
        await db.add(EmergencyCancel(user_id=UNKNOWN))
        await db.add(
            LecturerRating(
                id="rating",
                lecturer_id=EXISTING,
                participant_id=DELETED,
                skill_id="test",
                webinar_timestamp=utcnow(),
                webinar_name="test webinar",
                rating=None,
            )
        )


@pytest.fixture
def exists_user_patch(mocker: MockerFixture) -> AsyncMock:
    async def exists_user_uncached(user_id: str) -> bool | None:
        if user_id == EXISTING:
            return True
        if user_id == DELETED:
            return False
        raise InternalServiceError(MagicMock())

    return mocker.patch("api.sweep.exists_user_uncached", AsyncMock(side_effect=exists_user_uncached))


async def test__rate_limiter(mocker: MockerFixture) -> None:
    sleep_patch = mocker.patch("asyncio.sleep", AsyncMock())
    mocker.patch("api.sweep.time").monotonic = MagicMock(side_effect=[0.0, 0.1, 0.15])
    rate_limiter = RateLimiter(4)

    await rate_limiter.acquire()
    await rate_limiter.acquire()
    await rate_limiter.acquire()

    assert [c.args[0] for c in sleep_patch.call_args_list] == pytest.approx([0.15, 0.35])


async def test__rate_limiter__disabled(mocker: MockerFixture) -> None:
    sleep_patch = mocker.patch("asyncio.sleep", AsyncMock())

    await RateLimiter(0).acquire()

    sleep_patch.assert_not_called()


async def test__user_id_batch_query(data: None) -> None:
    async with db_context():
        assert await db.all(user_id_batch_query(None, 10)) == [EXISTING, DELETED, UNKNOWN]
        assert await db.all(user_id_batch_query(None, 2)) == [EXISTING, DELETED]
        assert await db.all(user_id_batch_query(DELETED, 10)) == [UNKNOWN]
        assert await db.all(user_id_batch_query(UNKNOWN, 10)) == []


async def test__sweep_deleted_users(data: None, exists_user_patch: AsyncMock) -> None:
    await sweep_deleted_users()

    assert sorted(c.args[0] for c in exists_user_patch.call_args_list) == [EXISTING, DELETED, UNKNOWN]

    async with db_context():
        # only the data of the user the auth service does not know anymore is deleted
        assert [c.user_id for c in await db.all(select(Coaching))] == []
        assert [e.user_id for e in await db.all(select(Exam))] == []
        assert [p.user_id for p in await db.all(select(WebinarParticipant))] == []
        assert [r.id for r in await db.all(select(LecturerRating))] == []

        assert [w.id for w in await db.all(select(Webinar))] == ["webinar"]
        assert [w.id for w in await db.all(select(WeeklySlot))] == ["weekly"]
        assert [e.user_id for e in await db.all(select(EmergencyCancel))] == [UNKNOWN]

        # the booking of the deleted user is cancelled, the slot itself belongs to another lecturer and is kept
        assert [(s.id, s.user_id, s.booked_by) for s in await db.all(select(Slot))] == [("slot", EXISTING, None)]


async def test__sweep_deleted_users__batched(
    data: None, exists_user_patch: AsyncMock, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "deleted_user_sweep_batch_size", 1)

    await sweep_deleted_users()

    assert sorted(c.args[0] for c in exists_user_patch.call_args_list) == [EXISTING, DELETED, UNKNOWN]

    async with db_context():
        assert [c.user_id for c in await db.all(select(Coaching))] == []
        assert [e.user_id for e in await db.all(select(EmergencyCancel))] == [UNKNOWN]


def test__main(mocker: MockerFixture) -> None:
    run_patch = mocker.patch("asyncio.run")
    sweep_patch = mocker.patch("api.sweep.sweep_deleted_users", MagicMock())

    main()

    run_patch.assert_called_once_with(sweep_patch())
