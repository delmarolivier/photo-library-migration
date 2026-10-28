"""Turning one answer per date batch into a date for every item in it."""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from photomigrate.analyse.date_audit import Dated
from photomigrate.analyse.date_fixes import AnswerError, Fix, fixes, planned_write
from photomigrate.catalogue import CatalogueItem
from photomigrate.ingest.exif import MetadataWrite

_ids = iter(range(1, 100_000))


def member(taken: datetime | None) -> Dated:
    item_id = next(_ids)
    return Dated(
        id=item_id, source="google", key=f"k{item_id}", name=f"IMG_{item_id:04d}.JPG",
        folder="Photos from 2019", camera=None, taken=taken, candidates={},
    )  # fmt: skip


A = member(datetime(2019, 7, 1, 12, 0))
B = member(datetime(2019, 7, 2, 8, 30, tzinfo=timezone(timedelta(hours=2))))
UNDATED = member(None)
SUGGESTED = {A.id: datetime(2019, 7, 1, 13, 0), B.id: None, UNDATED.id: datetime(2019, 7, 3)}


def test_a_year_alone_means_the_first_of_july_and_is_marked_approximate() -> None:
    found = fixes([A, UNDATED], {"answer": "year", "year": 2004}, SUGGESTED)

    assert found == [
        Fix(A.id, datetime(2004, 7, 1, 12, 0), approx=True),
        Fix(UNDATED.id, datetime(2004, 7, 1, 12, 0), approx=True),
    ]


def test_a_shift_moves_every_dated_item_by_exactly_the_offset() -> None:
    found = fixes([A, B, UNDATED], {"answer": "shift", "offset_seconds": -5400}, SUGGESTED)

    assert found == [
        Fix(A.id, datetime(2019, 7, 1, 10, 30), approx=False),
        Fix(B.id, datetime(2019, 7, 2, 7, 0, tzinfo=timezone(timedelta(hours=2))), approx=False),
    ]


_TIMES = st.datetimes(
    min_value=datetime(1950, 1, 1), max_value=datetime(2090, 1, 1)
) | st.datetimes(
    min_value=datetime(1950, 1, 1),
    max_value=datetime(2090, 1, 1),
    timezones=st.sampled_from([UTC, timezone(timedelta(hours=-5)), timezone(timedelta(hours=9))]),
)


@given(times=st.lists(_TIMES, min_size=1, max_size=8), seconds=st.integers(-(10**9), 10**9))
def test_any_shift_is_exact_for_every_item(times: list[datetime], seconds: int) -> None:
    members = [member(when) for when in times]

    found = fixes(members, {"answer": "shift", "offset_seconds": seconds}, {})

    assert [f.when for f in found] == [when + timedelta(seconds=seconds) for when in times]
    assert [f.when.tzinfo for f in found] == [when.tzinfo for when in times]
    assert not any(f.approx for f in found)


def test_accepting_takes_each_items_own_suggestion_and_skips_those_without() -> None:
    found = fixes([A, B, UNDATED], {"answer": "accept"}, SUGGESTED)

    assert found == [
        Fix(A.id, datetime(2019, 7, 1, 13, 0), approx=False),
        Fix(UNDATED.id, datetime(2019, 7, 3), approx=False),
    ]


def test_an_entered_date_goes_to_every_item() -> None:
    found = fixes([A, UNDATED], {"answer": "date", "date": "2018-12-24T18:30"}, SUGGESTED)

    assert [f.when for f in found] == [datetime(2018, 12, 24, 18, 30)] * 2
    assert fixes([A], {"answer": "date", "date": "2018-12-24"}, {})[0].when == datetime(
        2018, 12, 24
    )


def test_keeping_confirms_the_dates_items_have() -> None:
    found = fixes([A, B, UNDATED], {"answer": "keep"}, SUGGESTED)

    assert found == [Fix(A.id, A.taken, approx=False), Fix(B.id, B.taken, approx=False)]  # type: ignore[arg-type]


def test_removing_gives_no_dates() -> None:
    assert fixes([A, B, UNDATED], {"answer": "remove"}, SUGGESTED) == []


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ({}, "an answer needs one of"),
        ({"answer": "guess"}, "an answer needs one of"),
        ({"answer": "year"}, "a whole year"),
        ({"answer": "year", "year": "2004"}, "a whole year"),
        ({"answer": "year", "year": 1800}, "between 1900 and 2100"),
        ({"answer": "date", "date": "24/12/2018"}, "not a date"),
        ({"answer": "date"}, "not a date"),
        ({"answer": "shift", "offset_seconds": 1.5}, "whole number of seconds"),
        ({"answer": "shift", "offset_seconds": True}, "whole number of seconds"),
    ],
)
def test_answers_that_cannot_work_are_errors(answer: dict[str, object], message: str) -> None:
    with pytest.raises(AnswerError, match=message):
        fixes([A], answer, SUGGESTED)


ZONE = timezone(timedelta(hours=2))


def google_item(plan: dict[str, object]) -> CatalogueItem:
    return CatalogueItem(
        source="google", source_key="k", kind="photo", metadata_plan=json.dumps(plan)
    )


def test_import_writes_ingests_plan_when_no_date_was_given() -> None:
    plan = {"taken": "2019-07-01T12:00:00+00:00", "latitude": 1.0, "longitude": 2.0}

    written = planned_write(google_item(plan), {}, ZONE)

    assert written == MetadataWrite(datetime(2019, 7, 1, 12, 0, tzinfo=UTC), 1.0, 2.0)


def test_import_writes_the_given_date_in_the_home_zone_over_ingests_plan() -> None:
    plan = {"taken": "2019-07-01T12:00:00+00:00", "description": "Nice"}
    given_date = {"user": datetime(2004, 7, 1, 12, 0)}

    written = planned_write(google_item(plan), given_date, ZONE)

    assert written == MetadataWrite(
        taken=datetime(2004, 7, 1, 12, 0, tzinfo=ZONE), description="Nice"
    )
    shifted = {"user": datetime(2019, 7, 1, 13, 0, tzinfo=UTC)}
    assert planned_write(google_item({}), shifted, ZONE).taken == shifted["user"]
