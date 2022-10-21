from datetime import datetime, time

import pytest

from api.models.weekly_slots import next_slot


@pytest.mark.parametrize(
    "start,weekday,t,expected",
    [
        (datetime(2022, 10, 21, 8, 0), 4, time(10, 0), datetime(2022, 10, 21, 10, 0)),
        (datetime(2022, 10, 21, 8, 0), 4, time(6, 0), datetime(2022, 10, 28, 6, 0)),
        (datetime(2022, 10, 21, 8, 0), 6, time(10, 0), datetime(2022, 10, 23, 10, 0)),
        (datetime(2022, 10, 21, 8, 0), 6, time(6, 0), datetime(2022, 10, 23, 6, 0)),
        (datetime(2022, 10, 21, 8, 0), 1, time(10, 0), datetime(2022, 10, 25, 10, 0)),
        (datetime(2022, 10, 21, 8, 0), 1, time(6, 0), datetime(2022, 10, 25, 6, 0)),
        (datetime(2022, 10, 21, 8, 0), 4, time(8, 0), datetime(2022, 10, 28, 8, 0)),
    ],
)
def test__next_slot(start: datetime, weekday: int, t: time, expected: datetime) -> None:
    assert next_slot(start, weekday, t) == expected
