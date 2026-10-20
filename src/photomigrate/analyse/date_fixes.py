"""Date fixes: one answer per date batch becomes a date for every item in it (CLA-85).

The review gallery stores the answer as the batch's ``date`` decision. The answers:

* ``{"answer": "accept"}``: each item takes its own suggestion (items without one are left);
* ``{"answer": "keep"}``: the dates are right; each dated item keeps its date;
* ``{"answer": "date", "date": "2018-12-24T18:30"}``: every item takes that date;
* ``{"answer": "year", "year": 2004}``: every item takes 1 July of that year at noon, marked
  approximate (keyword ``date-approx``). Noon keeps the day the same in any time zone;
* ``{"answer": "shift", "offset_seconds": -5400}``: every dated item moves by exactly that
  much (a camera clock that was wrong), keeping its UTC offset if it has one.

Entered dates are local wall-clock time, like EXIF.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from photomigrate.analyse.date_audit import Dated

ANSWERS = ("accept", "keep", "date", "year", "shift")
APPROX_KEYWORD = "date-approx"


class AnswerError(ValueError):
    """A stored answer that cannot be applied."""


@dataclass(frozen=True)
class Fix:
    item_id: int
    when: datetime
    approx: bool


def _year(answer: Mapping[str, Any]) -> datetime:
    year = answer.get("year")
    if isinstance(year, bool) or not isinstance(year, int):
        raise AnswerError(f"a year answer needs a whole year, not {year!r}")
    if not 1900 <= year <= 2100:
        raise AnswerError(f"the year must be between 1900 and 2100, not {year}")
    return datetime(year, 7, 1, 12, 0)


def _date(answer: Mapping[str, Any]) -> datetime:
    try:
        return datetime.fromisoformat(str(answer["date"]))
    except (KeyError, ValueError):
        raise AnswerError(f"{answer.get('date')!r} is not a date (use 2018-12-24T18:30)") from None


def _offset(answer: Mapping[str, Any]) -> timedelta:
    seconds = answer.get("offset_seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise AnswerError(f"a shift needs a whole number of seconds, not {seconds!r}")
    return timedelta(seconds=seconds)


def fixes(
    members: Sequence[Dated],
    answer: Mapping[str, Any],
    suggested: Mapping[int, datetime | None],
) -> list[Fix]:
    """The new date of each member the answer gives one to, in member order."""
    kind = answer.get("answer")
    if kind == "year":
        when = _year(answer)
        return [Fix(m.id, when, approx=True) for m in members]
    if kind == "date":
        when = _date(answer)
        return [Fix(m.id, when, approx=False) for m in members]
    if kind == "shift":
        offset = _offset(answer)
        return [Fix(m.id, m.taken + offset, approx=False) for m in members if m.taken]
    if kind == "keep":
        return [Fix(m.id, m.taken, approx=False) for m in members if m.taken]
    if kind == "accept":
        found = [(m.id, suggested.get(m.id)) for m in members]
        return [Fix(item_id, when, approx=False) for item_id, when in found if when]
    raise AnswerError(f"an answer needs one of {', '.join(ANSWERS)} as 'answer', not {kind!r}")
