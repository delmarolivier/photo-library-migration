"""Comparing capture times when some carry a UTC offset and some are local wall-clock time."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from photomigrate.dates import moment_gap, same_moment

TAKEN = datetime(2019, 7, 1, 10, 34, 56, tzinfo=UTC)


@pytest.mark.parametrize(
    ("other", "tolerance", "same"),
    [
        (TAKEN + timedelta(seconds=1), 2, True),
        (TAKEN + timedelta(seconds=3), 2, False),
        (TAKEN.astimezone(timezone(timedelta(hours=2))), 2, True),
        (datetime(2019, 7, 1, 12, 34, 56), 2, True),  # naive local, UTC+2
        (datetime(2019, 7, 1, 5, 4, 57), 2, True),  # naive local, UTC-5:30
        (datetime(2019, 7, 1, 12, 34, 59), 2, False),
        (datetime(2019, 7, 1, 12, 34, 59), 3, True),
        (datetime(2019, 7, 1, 12, 41, 0), 2, False),  # not a quarter-hour offset
        (datetime(2019, 7, 2, 12, 34, 56), 2, False),  # beyond any time-zone offset
    ],
)
def test_same_moment(other: datetime, tolerance: int, same: bool) -> None:
    assert same_moment(other, TAKEN, timedelta(seconds=tolerance)) is same
    assert same_moment(TAKEN, other, timedelta(seconds=tolerance)) is same


def test_two_naive_times_compare_directly() -> None:
    assert same_moment(
        datetime(2019, 7, 1, 12), datetime(2019, 7, 1, 12, 0, 2), timedelta(seconds=2)
    )
    assert not same_moment(datetime(2019, 7, 1, 12), datetime(2019, 7, 1, 14), timedelta(seconds=2))


def test_moment_gap_measures_the_residual_after_a_time_zone_offset() -> None:
    assert moment_gap(datetime(2019, 7, 1, 12, 35, 0), TAKEN) == timedelta(seconds=4)
    assert moment_gap(TAKEN, TAKEN + timedelta(seconds=9)) == timedelta(seconds=9)
    assert moment_gap(datetime(2019, 7, 3, 10, 34, 56), TAKEN) == timedelta(days=2)
