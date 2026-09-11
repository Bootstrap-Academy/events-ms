"""The committed reader is reserved inside the configured finite DB budget."""

from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture

from api.database.database import DB


@pytest.mark.parametrize("size,overflow,general", [(20, 20, (20, 19)), (2, 0, (1, 0)), (1, 1, (1, 0))])
async def test_reader_reserves_existing_budget_with_no_reader_overflow(
    mocker: MockerFixture, size: int, overflow: int, general: tuple[int, int]
) -> None:
    create = mocker.patch("api.database.database.create_async_engine")
    DB("synthetic", reserve_committed_reader=True, pool_size=size, max_overflow=overflow)
    calls = create.call_args_list
    assert len(calls) == 2
    assert (calls[0].kwargs["pool_size"], calls[0].kwargs["max_overflow"]) == (1, 0)
    assert (calls[1].kwargs["pool_size"], calls[1].kwargs["max_overflow"]) == general
    assert sum(general) + 1 == size + overflow


@pytest.mark.parametrize("size,overflow", [(1, 0), (0, 1), (20, -1)])
async def test_impossible_or_unbounded_budget_is_not_silently_increased(
    mocker: MockerFixture, size: int, overflow: int
) -> None:
    create = mocker.patch("api.database.database.create_async_engine")
    with pytest.raises(ValueError):
        DB("synthetic", reserve_committed_reader=True, pool_size=size, max_overflow=overflow)
    create.assert_not_called()


async def test_both_pools_are_disposed(mocker: MockerFixture) -> None:
    general, reader = mocker.Mock(), mocker.Mock()
    general.dispose = AsyncMock()
    reader.dispose = AsyncMock()
    value = object.__new__(DB)
    value.engine, value.committed_read_engine = general, reader
    await value.dispose()
    general.dispose.assert_awaited_once()
    reader.dispose.assert_awaited_once()
