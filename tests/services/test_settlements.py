"""Failure injection over real event transactions and the real local HTTP coin ledger.

Run with T6_BACKEND_URL, T6_BACKEND_DB and T6_EVENTS_DB pointing exclusively at
isolated synthetic services. Ordinary suite runs skip these integration cases.
"""

import asyncio
import os
from datetime import timedelta
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import create_async_engine

from api.database import Base, db, db_context, select
from api.endpoints.calendar import cancel_event
from api.models import CoinOperation, SettlementBatch, SettlementClaim, Webinar, WebinarParticipant
from api.schemas.user import User
from api.services import shop
from api.services.internal import InternalService
from api.services.settlements import recover_settlements
from api.services.user_deletion import delete_user_data
from api.settings import settings
from api.utils.jwt import encode_jwt
from api.utils.utc import utcnow
from tests.payment_fixtures import paid_participant


FOO = "a8d95e0f-71ae-4c49-995e-695b7c93848c"
BAR = "94d0e3ca-bf16-486b-a172-b87f4bcbd039"
HOST = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
async def ledger(mocker: MockerFixture) -> AsyncIterator[Any]:
    if not os.getenv("T6_BACKEND_URL"):
        pytest.skip("requires isolated synthetic backend and PostgreSQL")
    engine = create_async_engine(os.environ["T6_EVENTS_DB"])
    mocker.patch.object(db, "engine", engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    mocker.patch.object(
        InternalService,
        "client",
        new_callable=property,
        fget=lambda _: AsyncClient(
            base_url=os.environ["T6_BACKEND_URL"],
            headers={
                "Authorization": encode_jwt({"aud": "shop"}, timedelta(minutes=5), secret="synthetic-T6-local-test")
            },
        ),
    )
    mocker.patch.object(settings, "jwt_secret", "synthetic-T6-local-test")
    mocker.patch("api.endpoints.calendar.clear_cache", AsyncMock())
    mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    mocker.patch("api.utils.email.notify", AsyncMock())
    connection = await asyncpg.connect(os.environ["T6_BACKEND_DB"])
    yield connection
    await connection.close()
    await engine.dispose()


async def seed() -> None:
    async with db_context():
        await db.add(
            Webinar(
                id="webinar",
                skill_id="test",
                creator=HOST,
                creation_date=utcnow(),
                name="Synthetic T6",
                description="Synthetic",
                admin_link="local",
                link="local",
                start=utcnow() + timedelta(days=14),
                end=utcnow() + timedelta(days=14, hours=1),
                max_participants=5,
                price=100,
            )
        )
        for user_id in [FOO, BAR]:
            await db.add(paid_participant(webinar_id="webinar", user_id=user_id, paid_coins=100))


async def cancel() -> None:
    async with db_context():
        assert await cancel_event("webinar", User(id=HOST, email_verified=True, admin=False)) is True


async def assert_settled(ledger: Any) -> None:
    async with db_context():
        operations = await db.all(select(CoinOperation))
        assert len(operations) == 2
        for operation in operations:
            assert operation.completed_at is not None
            row = await ledger.fetchrow("SELECT * FROM internal_coin_operations WHERE id=$1", UUID(operation.id))
            assert row is not None and row["coins"] == 100
        assert await db.all(select(Webinar)) == []


@pytest.mark.parametrize("failure", ["false", "exception", "lost_response"])
async def test_partial_credit_replay(ledger: Any, mocker: MockerFixture, failure: str) -> None:
    await seed()
    before = await ledger.fetchval("SELECT count(*) FROM transactions")
    real = shop.apply_coin_operation
    calls = 0

    async def flaky(*args: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            if failure == "false":
                return False
            if failure == "lost_response":
                assert await real(*args)
            raise RuntimeError("synthetic transport failure")
        return await real(*args)

    mocker.patch.object(shop, "apply_coin_operation", side_effect=flaky)
    with pytest.raises(HTTPException) as err:
        await cancel()
    assert err.value.status_code == 503
    assert isinstance(err.value.detail, dict)
    assert err.value.detail["pending_operations"] == 1
    async with db_context():
        assert await db.all(select(Webinar)) == []
        assert len(await db.all(select(CoinOperation))) == 2
    await cancel()
    await cancel()
    await assert_settled(ledger)
    assert await ledger.fetchval("SELECT count(*) FROM transactions") == before + 2


@pytest.mark.parametrize("commit_number", [1, 2])
async def test_event_commit_failure(ledger: Any, mocker: MockerFixture, commit_number: int) -> None:
    await seed()
    before = await ledger.fetchval("SELECT count(*) FROM transactions")
    real_commit = db.commit
    calls = 0

    async def failing_commit() -> None:
        nonlocal calls
        calls += 1
        if calls == commit_number:
            raise RuntimeError("synthetic event commit failure")
        await real_commit()

    mocker.patch.object(db, "commit", side_effect=failing_commit)
    with pytest.raises(RuntimeError):
        await cancel()
    async with db_context():
        assert (await db.get(Webinar, id="webinar") is not None) == (commit_number == 1)
    await cancel()
    await assert_settled(ledger)
    assert await ledger.fetchval("SELECT count(*) FROM transactions") == before + 2


async def test_concurrent_cancellation_and_workers(ledger: Any, mocker: MockerFixture) -> None:
    await seed()
    before = await ledger.fetchval("SELECT count(*) FROM transactions")
    results = await asyncio.gather(cancel(), cancel(), return_exceptions=True)
    assert all(result is None for result in results)
    await asyncio.gather(recover_settlements(), recover_settlements())
    await assert_settled(ledger)
    assert await ledger.fetchval("SELECT count(*) FROM transactions") == before + 2
    async with db_context():
        assert len(await db.all(select(SettlementBatch))) == 1


async def test_deletion_retains_missing_recipient_obligation(ledger: Any, mocker: MockerFixture) -> None:
    await seed()
    async with db_context():
        participant = await db.get(WebinarParticipant, webinar_id="webinar", user_id=BAR)
        assert participant is not None
        participant.user_id = str(uuid4())
    with pytest.raises(HTTPException):
        async with db_context():
            await delete_user_data(HOST)
    await recover_settlements()
    async with db_context():
        operations = await db.all(select(CoinOperation))
        assert len(operations) == 2
        assert len([item for item in operations if item.completed_at is None]) == 1
        assert await db.all(select(Webinar)) == []


async def test_shop_conflict_concurrency_and_commit_failure(ledger: Any) -> None:
    before = await ledger.fetchval("SELECT count(*) FROM transactions")
    operation = str(uuid4())
    args = (operation, FOO, 17, "Synthetic T6 immutable request", False)
    # The deferred trigger fails precisely at COMMIT, after balance+ledger writes.
    await ledger.execute(
        f"""
        CREATE FUNCTION t6_reject_commit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id = '{operation}'::uuid THEN RAISE EXCEPTION 'synthetic commit failure'; END IF;
            RETURN NEW;
        END $$;
        CREATE CONSTRAINT TRIGGER t6_reject_commit AFTER INSERT ON internal_coin_operations
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION t6_reject_commit();
    """
    )
    try:
        assert await shop.apply_coin_operation(*args) is False
        assert await ledger.fetchval("SELECT count(*) FROM internal_coin_operations WHERE id=$1", UUID(operation)) == 0
        assert await ledger.fetchval("SELECT count(*) FROM transactions") == before
    finally:
        await ledger.execute(
            "DROP TRIGGER t6_reject_commit ON internal_coin_operations; DROP FUNCTION t6_reject_commit()"
        )
    assert await asyncio.gather(*[shop.apply_coin_operation(*args) for _ in range(8)]) == [True] * 8
    assert await ledger.fetchval("SELECT count(*) FROM transactions") == before + 1
    for altered in [
        (operation, BAR, 17, args[3], False),
        (operation, FOO, 18, args[3], False),
        (operation, FOO, 17, "changed", False),
        (operation, FOO, 17, args[3], True),
    ]:
        assert await shop.apply_coin_operation(*altered) is False
    assert await ledger.fetchval("SELECT count(*) FROM transactions") == before + 1


async def test_independent_cancel_rebook_cycle(ledger: Any) -> None:
    before = await ledger.fetchval("SELECT count(*) FROM transactions")
    await seed()
    await cancel()
    await seed()
    await cancel()
    async with db_context():
        operations = await db.all(select(CoinOperation))
        assert len(operations) == len({item.id for item in operations}) == 4
        assert all(item.completed_at for item in operations)
    assert await ledger.fetchval("SELECT count(*) FROM transactions") == before + 4


async def test_events_additive_migration_and_evidence_guard(ledger: Any) -> None:
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from sqlalchemy import text

    migration_path = (
        Path(__file__).parents[2] / "alembic/versions/2026_09_07_1700-c6e1700ab001_add_durable_settlements.py"
    )
    spec = importlib.util.spec_from_file_location("t6_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def roundtrip(conn: Any) -> None:
        SettlementClaim.__table__.drop(conn)
        CoinOperation.__table__.drop(conn)
        SettlementBatch.__table__.drop(conn)
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
        CoinOperation.__table__.drop(conn)
        CoinOperation.__table__.create(conn)
        SettlementClaim.__table__.create(conn)

    async with db.engine.begin() as conn:
        await conn.run_sync(roundtrip)
    await seed()
    await cancel()
    async with db.engine.begin() as conn:

        def refuse(conn: Any) -> None:
            with Operations.context(MigrationContext.configure(conn)):
                with pytest.raises(RuntimeError, match="evidence"):
                    migration.downgrade()

        await conn.run_sync(refuse)
        assert (await conn.execute(text("SELECT count(*) FROM events_coin_operations"))).scalar() == 2
