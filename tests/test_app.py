import asyncio
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from _pytest.monkeypatch import MonkeyPatch
from httpx import AsyncClient
from pytest_mock import MockerFixture

from ._utils import import_module, mock_asynccontextmanager
from api import app
from api.database import db


def get_decorated_function(
    fastapi_patch: MagicMock, decorator_name: str, *decorator_args: Any, **decorator_kwargs: Any
) -> tuple[Any, Callable[..., Any]]:
    functions: list[Callable[..., Any]] = []
    decorator = MagicMock(side_effect=functions.append)
    getattr(fastapi_patch(), decorator_name).side_effect = lambda *args, **kwargs: (
        decorator if (args, kwargs) == (decorator_args, decorator_kwargs) else MagicMock()
    )
    fastapi_patch.reset_mock()

    module = import_module(app)

    decorator.assert_called_once()
    assert len(functions) == 1
    return module, functions[0]


async def test__db_session(mocker: MockerFixture) -> None:
    fastapi_patch = mocker.patch("fastapi.FastAPI")
    expected = MagicMock()
    request = MagicMock()

    module, db_session = get_decorated_function(fastapi_patch, "middleware", "http")

    module.db_context, [func_callback], assert_calls = mock_asynccontextmanager(1, None)
    call_next = AsyncMock(side_effect=lambda _: func_callback() or expected)

    result = await db_session(request, call_next)

    assert_calls()
    call_next.assert_called_once_with(request)
    assert result == expected


async def test__rollback_on_exception(mocker: MockerFixture) -> None:
    fastapi_patch = mocker.patch("fastapi.FastAPI")
    db_patch = mocker.patch("api.database.db")
    db_patch.session.rollback = AsyncMock()
    http_exception_patch = mocker.patch("starlette.exceptions.HTTPException")
    http_exception_handler_patch = mocker.patch("fastapi.exception_handlers.http_exception_handler", AsyncMock())

    _, rollback_on_exception = get_decorated_function(fastapi_patch, "exception_handler", http_exception_patch)

    result = await rollback_on_exception(request := MagicMock(), exc := MagicMock())

    db_patch.session.rollback.assert_called_once_with()
    http_exception_handler_patch.assert_called_once_with(request, exc)
    assert result == await http_exception_handler_patch()


async def test__on_startup(mocker: MockerFixture, monkeypatch: MonkeyPatch) -> None:
    fastapi_patch = mocker.patch("fastapi.FastAPI")
    db_patch = mocker.patch("api.database.db")

    module, on_startup = get_decorated_function(fastapi_patch, "on_event", "startup")
    db_patch.create_tables = AsyncMock()

    await on_startup()

    db_patch.create_tables.assert_not_called()  # use alembic migrations instead
    tasks = [module.app.state.cleanup_task, module.app.state.confirmation_task, module.app.state.benefit_task]
    assert len(set(tasks)) == 3 and all(isinstance(task, asyncio.Task) for task in tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert all(task.done() and task.cancelled() for task in tasks)


async def test__on_shutdown(mocker: MockerFixture) -> None:
    fastapi_patch = mocker.patch("fastapi.FastAPI")

    module, on_shutdown = get_decorated_function(fastapi_patch, "on_event", "shutdown")
    tasks = [asyncio.create_task(asyncio.sleep(30)) for _ in range(3)]
    module.app.state.cleanup_task, module.app.state.confirmation_task, module.app.state.benefit_task = tasks
    await on_shutdown()
    assert all(task.cancelled() for task in tasks)


async def test__status(client: AsyncClient) -> None:
    response = await client.head("/status")
    assert response.status_code == 200


@pytest.mark.parametrize("family", ["ordinary", "retained"])
@pytest.mark.parametrize("failure", ["unavailable", "deadline"])
async def test_confirmation_and_other_cancellation_family_progress_after_failure(
    mocker: MockerFixture, family: str, failure: str
) -> None:
    confirmation_seen, failed_released = asyncio.Event(), asyncio.Event()
    trace: list[str] = []
    deadlines: list[float] = []
    original_timeout = asyncio.timeout

    def bounded_timeout(seconds: float) -> asyncio.Timeout:
        deadlines.append(seconds)
        return original_timeout(0.01)

    async def cancellation(name: str) -> None:
        trace.append(name + ":entered")
        if name != family:
            trace.append(name + ":complete")
            return
        try:
            if failure == "unavailable":
                raise OSError("synthetic original cancellation inventory unavailable")
            await asyncio.Event().wait()
        finally:
            trace.append(name + ":released")
            failed_released.set()

    async def confirmation() -> None:
        trace.append("confirmation:complete")
        confirmation_seen.set()

    async def recover_ordinary() -> None:
        await cancellation("ordinary")

    async def recover_retained() -> None:
        await cancellation("retained")

    ordinary = mocker.patch("api.services.ordinary_cancellations.recover", side_effect=recover_ordinary)
    retained = mocker.patch("api.services.event_cancellations.recover", side_effect=recover_retained)
    confirmed = mocker.patch("api.services.booking_contracts.recover", side_effect=confirmation)
    mocker.patch.object(asyncio, "timeout", side_effect=bounded_timeout)
    task = asyncio.create_task(app.confirmation_loop())
    try:
        await asyncio.wait_for(confirmation_seen.wait(), 1)
        healthy = "retained" if family == "ordinary" else "ordinary"
        assert failed_released.is_set()
        assert healthy + ":complete" in trace
        assert trace.index(family + ":released") < trace.index("confirmation:complete")
        assert trace.index(healthy + ":complete") < trace.index("confirmation:complete")
        assert len(deadlines) == 2 and all(0 < seconds <= 10 for seconds in deadlines)
        ordinary.assert_awaited_once_with()
        retained.assert_awaited_once_with()
        confirmed.assert_awaited_once_with()
    finally:
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
    assert task.cancelled()


async def test_confirmation_failure_does_not_starve_either_cancellation_family_in_next_cycle(
    mocker: MockerFixture,
) -> None:
    second_confirmation = asyncio.Event()
    trace: list[str] = []
    ordinary = mocker.patch("api.services.ordinary_cancellations.recover", new_callable=AsyncMock)
    retained = mocker.patch("api.services.event_cancellations.recover", new_callable=AsyncMock)
    ordinary.side_effect = lambda: trace.append("ordinary")
    retained.side_effect = lambda: trace.append("retained")

    async def confirmation() -> None:
        trace.append("confirmation")
        if trace.count("confirmation") == 1:
            raise OSError("synthetic confirmation failure")
        second_confirmation.set()

    pauses = []

    async def next_cycle(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) > 1:
            await asyncio.Event().wait()

    mocker.patch("api.services.booking_contracts.recover", side_effect=confirmation)
    mocker.patch.object(asyncio, "sleep", side_effect=next_cycle)
    task = asyncio.create_task(app.confirmation_loop())
    try:
        await asyncio.wait_for(second_confirmation.wait(), 1)
        assert trace == ["ordinary", "retained", "confirmation"] * 2
        assert pauses == [30, 30]
    finally:
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
    assert task.cancelled()


@pytest.mark.parametrize("active", ["ordinary", "retained", "confirmation", "between_cycles"])
async def test_actual_shutdown_releases_active_family_and_all_companion_tasks(
    mocker: MockerFixture, active: str
) -> None:
    entered, released = asyncio.Event(), asyncio.Event()
    trace: list[str] = []

    async def stage(name: str) -> None:
        trace.append(name)
        if name == active:
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                released.set()

    # Every background call is an in-process ordinary fixture, including the
    # healthy earlier families. No real service operation or application server.
    async def ordinary() -> None:
        await stage("ordinary")

    async def retained() -> None:
        await stage("retained")

    async def confirmation() -> None:
        await stage("confirmation")

    async def pause(seconds: float) -> None:
        assert seconds == 30
        await stage("between_cycles")

    mocker.patch("api.services.ordinary_cancellations.recover", side_effect=ordinary)
    mocker.patch("api.services.event_cancellations.recover", side_effect=retained)
    mocker.patch("api.services.booking_contracts.recover", side_effect=confirmation)
    mocker.patch.object(asyncio, "sleep", side_effect=pause)
    disposed = mocker.patch.object(db, "dispose", new_callable=AsyncMock)
    confirmation_task = asyncio.create_task(app.confirmation_loop())
    companions = [asyncio.create_task(asyncio.Event().wait()) for _ in range(2)]
    mocker.patch.object(app.app.state, "cleanup_task", companions[0], create=True)
    mocker.patch.object(app.app.state, "confirmation_task", confirmation_task, create=True)
    mocker.patch.object(app.app.state, "benefit_task", companions[1], create=True)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(app.on_shutdown(), 1)
    finally:
        for task in [confirmation_task, *companions]:
            task.cancel()
        await asyncio.gather(confirmation_task, *companions, return_exceptions=True)
    expected = ["ordinary", "retained", "confirmation", "between_cycles"]
    assert trace == expected[: expected.index(active) + 1]
    assert released.is_set() and all(task.cancelled() for task in [confirmation_task, *companions])
    disposed.assert_awaited_once_with()
