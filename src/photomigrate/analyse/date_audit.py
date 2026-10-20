"""Date audit: find doubtful dates, batch them, suggest fixes, so the review can ask (CLA-85).

Every item's date candidates (catalogue ``date_candidates``) are compared. An item is flagged
with one or more **reasons**:

* ``missing``: it has no capture date at all;
* ``placeholder``: its file (EXIF, QuickTime or Photos) says 1970-01-01, 1980-01-01 or
  2000-01-01, or only a placeholder sidecar date is left;
* ``future``: its date is after today (with a day's grace for local times);
* ``upload-as-taken``: without a file date, the sidecar's capture time is the upload time,
  which is Google's fallback for "unknown";
* ``conflict``: two capture clues (file, sidecar capture time, Photos date, file name) are more
  than a day apart. Upload times and file mtimes are never capture clues;
* ``out-of-sequence``: its camera's shots numbered just before and after it agree with each
  other, and it is more than a day outside them.

An item with a date the user gave (candidate ``user``) is never flagged again.

**Sequences** are one source's shots from one camera with one name prefix (``IMG_``, ``DSC``),
in name order. **Batches** gather flagged items by problem (``undated``: no date to keep;
``doubtful``: a date that may be wrong), camera, folder or album, and name prefix, so one
answer covers a whole run. A batch's key names the batch, not its members.

**Suggestions**, per item, best first: the date in its file name; else a date interpolated
between its sequence's nearest well-dated shots (by shot number), or the one on its side;
else a plausible sidecar capture time. A doubtful batch whose members are all off from their
suggestions by the same amount (to the minute) also suggests that **offset**: a wrong clock.
"""

from __future__ import annotations

import hashlib
import itertools
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from photomigrate.catalogue import Source
from photomigrate.dates import moment_gap
from photomigrate.ingest.names import parse_media_name
from photomigrate.ingest.policy import is_placeholder

Reason = Literal[
    "missing", "placeholder", "future", "upload-as-taken", "conflict", "out-of-sequence"
]
Problem = Literal["undated", "doubtful"]

DAY = timedelta(days=1)
_SAME_INSTANT = timedelta(seconds=2)
_FILE_SOURCES = ("exif", "quicktime", "icloud")
_SEQUENCE = re.compile(r"^(?P<prefix>\D*)(?P<number>\d+)")


@dataclass(frozen=True)
class Dated:
    """An item's date facts. ``taken`` is the date it has now (or will be written)."""

    id: int
    source: Source
    key: str
    name: str
    folder: str
    """The Takeout folder (year or album), or the iCloud library."""
    camera: str | None
    taken: datetime | None
    candidates: Mapping[str, datetime] = field(compare=False)


def _wall(when: datetime) -> datetime:
    return when.replace(tzinfo=None)


def _upload_as_taken(item: Dated) -> bool:
    taken, created = item.candidates.get("sidecar-taken"), item.candidates.get("sidecar-created")
    return taken is not None and created is not None and abs(taken - created) <= _SAME_INSTANT


def _capture_clues(item: Dated) -> list[datetime]:
    """Capture times from every source that can know one, placeholders and guesses left out."""
    clues = [item.candidates[s] for s in (*_FILE_SOURCES, "filename") if s in item.candidates]
    sidecar = item.candidates.get("sidecar-taken")
    if sidecar is not None and not _upload_as_taken(item):
        clues.append(sidecar)
    return [when for when in clues if not is_placeholder(when)]


def item_reasons(item: Dated, now: datetime) -> set[Reason]:
    """Why the item's date is doubtful (everything but ``out-of-sequence``)."""
    if "user" in item.candidates:
        return set()
    reasons: set[Reason] = set()
    file_dates = [item.candidates[s] for s in _FILE_SOURCES if s in item.candidates]
    if item.taken is None:
        reasons.add("missing")
        sidecar = item.candidates.get("sidecar-taken")
        if sidecar is not None and is_placeholder(sidecar) and not file_dates:
            reasons.add("placeholder")
        if not file_dates and _upload_as_taken(item):
            reasons.add("upload-as-taken")
    elif _wall(item.taken) > _wall(now) + DAY:
        reasons.add("future")
    if any(is_placeholder(when) for when in file_dates):
        reasons.add("placeholder")
    clues = _capture_clues(item)
    if any(moment_gap(a, b) > DAY for a, b in itertools.combinations(clues, 2)):
        reasons.add("conflict")
    return reasons


def sequence_of(name: str) -> tuple[str, int] | None:
    """The name's prefix and shot number (``IMG_1234(1)-edited.JPG``: ``IMG_``, 1234)."""
    parsed = parse_media_name(name)
    found = _SEQUENCE.match(parsed.edited or parsed.stem)
    return None if found is None else (found["prefix"], int(found["number"]))


def _sequences(items: Iterable[Dated]) -> list[list[Dated]]:
    """Each sequence's items in shot order."""
    runs: dict[tuple[str, str, str], list[Dated]] = defaultdict(list)
    for current in items:
        found = sequence_of(current.name)
        if found is not None:
            runs[(current.source, current.camera or "", found[0])].append(current)
    return [sorted(run, key=_order) for _, run in sorted(runs.items())]


def _order(item: Dated) -> tuple[int, str]:
    found = sequence_of(item.name)
    return (found[1] if found else 0, item.name)


def _datable(item: Dated) -> bool:
    return item.taken is not None and not is_placeholder(item.taken)


def out_of_sequence(items: Iterable[Dated]) -> set[int]:
    """Items more than a day outside the dates of their sequence neighbours, when those two
    neighbours agree with each other."""
    found = set()
    for run in _sequences(items):
        dated = [i for i in run if _datable(i)]
        for before, current, after in zip(dated, dated[1:], dated[2:], strict=False):
            low, when, high = (_wall(i.taken) for i in (before, current, after) if i.taken)
            if low <= high + DAY and not low - DAY <= when <= high + DAY:
                found.add(current.id)
    return found


def audit(items: Sequence[Dated], now: datetime) -> dict[int, set[Reason]]:
    """Every flagged item's reasons."""
    found = {i.id: item_reasons(i, now) for i in items}
    for item_id in out_of_sequence(i for i in items if "user" not in i.candidates):
        found[item_id].add("out-of-sequence")
    return {item_id: reasons for item_id, reasons in found.items() if reasons}


# --- suggestions ---------------------------------------------------------------------------


def _between(before: Dated, after: Dated, current: Dated) -> datetime:
    low, high = _wall(before.taken or datetime.min), _wall(after.taken or datetime.min)
    start, at, end = (_order(i)[0] for i in (before, current, after))
    share = (at - start) / (end - start) if end != start else 0.5
    return low + (high - low) * share


def suggestions(
    items: Sequence[Dated], flagged: Mapping[int, set[Reason]]
) -> dict[int, datetime | None]:
    """A suggested date for every flagged item (``None`` when there is no clue)."""
    found: dict[int, datetime | None] = {}
    for run in _sequences(items):
        good = [i for i in run if i.id not in flagged and _datable(i)]
        for current in run:
            if current.id not in flagged:
                continue
            at = _order(current)
            before = [i for i in good if _order(i) < at]
            after = [i for i in good if _order(i) > at]
            if before and after:
                found[current.id] = _between(before[-1], after[0], current)
            elif before or after:
                found[current.id] = _wall(
                    (before[-1] if before else after[0]).taken or datetime.min
                )
    result: dict[int, datetime | None] = {}
    for current in items:
        if current.id not in flagged:
            continue
        in_name = current.candidates.get("filename")
        sidecar = current.candidates.get("sidecar-taken")
        if sidecar is not None and (_upload_as_taken(current) or is_placeholder(sidecar)):
            sidecar = None
        result[current.id] = in_name or found.get(current.id) or sidecar
    return result


# --- batches -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Batch:
    problem: Problem
    camera: str | None
    folder: str
    prefix: str
    members: tuple[Dated, ...]
    """In shot order."""
    reasons: Mapping[int, frozenset[Reason]]
    suggestions: Mapping[int, datetime | None]
    offset: timedelta | None
    """The one gap between every member's date and its suggestion, if there is one."""

    @property
    def key(self) -> str:
        names = "|".join([self.problem, self.camera or "", self.folder, self.prefix])
        return "dates-" + hashlib.sha256(names.encode()).hexdigest()[:16]


def _offset(members: Sequence[Dated], suggested: Mapping[int, datetime | None]) -> timedelta | None:
    gaps = set()
    for member in members:
        target = suggested.get(member.id)
        if member.taken is None or target is None:
            return None
        gap = _wall(target) - _wall(member.taken)
        gaps.add(timedelta(minutes=round(gap.total_seconds() / 60)))
    return gaps.pop() if len(gaps) == 1 else None


def batches(
    items: Sequence[Dated],
    flagged: Mapping[int, set[Reason]],
    suggested: Mapping[int, datetime | None] | None = None,
) -> list[Batch]:
    suggested = suggested or {}
    grouped: dict[tuple[Problem, str, str, str], list[Dated]] = defaultdict(list)
    for current in items:
        if current.id in flagged:
            problem: Problem = "undated" if current.taken is None else "doubtful"
            prefix = (sequence_of(current.name) or ("",))[0]
            grouped[(problem, current.camera or "", current.folder, prefix)].append(current)
    result = []
    for (problem, camera, folder, prefix), members in sorted(grouped.items()):
        ordered = tuple(sorted(members, key=_order))
        result.append(
            Batch(
                problem=problem,
                camera=camera or None,
                folder=folder,
                prefix=prefix,
                members=ordered,
                reasons={m.id: frozenset(flagged[m.id]) for m in ordered},
                suggestions={m.id: suggested.get(m.id) for m in ordered},
                offset=_offset(ordered, suggested) if problem == "doubtful" else None,
            )
        )
    return result
