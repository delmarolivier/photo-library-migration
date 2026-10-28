"""Date fixes: one answer per date batch becomes a date for every item in it (CLA-85).

The review gallery stores the answer as the batch's ``date`` decision. The answers:

* ``{"answer": "accept"}``: each item takes its own suggestion (items without one are left);
* ``{"answer": "keep"}``: the dates are right; each dated item keeps its date;
* ``{"answer": "date", "date": "2018-12-24T18:30"}``: every item takes that date;
* ``{"answer": "year", "year": 2004}``: every item takes 1 July of that year at noon, marked
  approximate (keyword ``date-approx``). Noon keeps the day the same in any time zone;
* ``{"answer": "shift", "offset_seconds": -5400}``: every dated item moves by exactly that
  much (a camera clock that was wrong), keeping its UTC offset if it has one;
* ``{"answer": "remove"}``: the batch is junk; no dates change, and no item in it is
  imported (``importer.outcomes``; iCloud items go to "To Delete").

Entered dates are local wall-clock time, like EXIF.

**The stage** (``date-fixes``, run by ``analyse`` before the audit) turns each answered batch
into one action per item: ``fix-date`` for Google items, ``plan-timewarp`` for iCloud items
(osxphotos timewarp runs at import, M4). Either way the new date is stored as the item's
``user`` date candidate, and ``date-approx`` is its keyword exactly when the date is a year
guess. Nothing else changes: the importer writes a Google working copy from
``planned_write``, which puts that date over ingest's plan. Answers are worked out from the
dates the batch was shown with, so applying one again changes nothing. An answer that cannot
work becomes a ``bad-answer`` action in the plan, and the other batches still apply.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any, cast

from photomigrate.analyse.date_audit import Dated, dated_items, name_of
from photomigrate.catalogue import Catalogue, CatalogueItem, Source
from photomigrate.ingest.exif import MetadataWrite
from photomigrate.runner import Action, JSONValue

ANSWERS = ("accept", "keep", "date", "year", "shift", "remove")
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
    if kind == "remove":
        return []
    raise AnswerError(f"an answer needs one of {', '.join(ANSWERS)} as 'answer', not {kind!r}")


def planned_write(
    item: CatalogueItem, candidates: Mapping[str, datetime], zone: tzinfo
) -> MetadataWrite:
    """What import writes into a Google item's working copy: ingest's plan, with the date the
    review gave (in the home ``zone`` when it is a bare local time) instead of its own."""
    plan = MetadataWrite.from_dict(json.loads(item.metadata_plan))
    given = candidates.get("user")
    if given is None:
        return plan
    return dataclasses.replace(plan, taken=given if given.tzinfo else given.replace(tzinfo=zone))


class DateFixStage:
    name = "date-fixes"

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        by_id = {item.id: item for item in dated_items(catalogue, answered=False)}
        for group in catalogue.groups("date-batch"):
            answer = catalogue.decision("group", group.id, "date")
            if answer is None:
                continue
            members = [by_id[i] for i, _, _ in catalogue.group_members(group.id) if i in by_id]
            shown = cast(dict[str, str | None], group.details.get("suggestions", {}))
            suggested = {
                m.id: datetime.fromisoformat(text)
                for m in members
                if (text := shown.get(name_of(m)))
            }
            try:
                found = fixes(members, answer, suggested)
            except AnswerError as exc:
                yield Action("bad-answer", group.key, {"answer": answer, "error": str(exc)})
                continue
            for fix in found:
                item = by_id[fix.item_id]
                details: dict[str, JSONValue] = {
                    "source": item.source,
                    "key": item.key,
                    "from": None if item.taken is None else item.taken.isoformat(),
                    "to": fix.when.isoformat(),
                    "approx": fix.approx,
                    "batch": group.key,
                }
                kind = "fix-date" if item.source == "google" else "plan-timewarp"
                yield Action(kind, name_of(item), details)

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        if action.kind == "bad-answer":
            return  # reported in the plan; the review needs to answer again
        details = action.details
        item_id = catalogue.item_for_key(cast(Source, details["source"]), str(details["key"]))
        if item_id is None:
            raise KeyError(f"{action.target} is no longer in the catalogue")
        catalogue.set_date_candidate(item_id, "user", datetime.fromisoformat(str(details["to"])))
        catalogue.set_keywords(
            item_id, APPROX_KEYWORD, [APPROX_KEYWORD] if details["approx"] else []
        )
