"""Google Maps Timeline: where the phone was at a moment, for location backfill (CLA-87).

Delmar's export is the **iOS on-device format** (Maps > Your Timeline > Export): a JSON list of
segments, each with ``startTime`` and ``endTime`` (ISO times with an offset) and one of:

* ``visit.topCandidate.placeLocation``: the place the phone stayed at for the whole segment;
* ``activity.start`` and ``.end``: where a journey began and ended;
* ``timelinePath[]``: points along the way, each ``durationMinutesOffsetFromStartTime`` after
  the segment's start.

Places are ``geo:lat,lon`` strings, and every number is a string. ``locate`` answers with the
shortest visit that covers the moment. Otherwise it interpolates between the path and
activity points just before and after the moment, when both are within the tolerance, or
takes the one that is. Other segment kinds are ignored.
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal


class TimelineError(ValueError):
    """The Timeline export is missing or not in the expected format."""


@dataclass(frozen=True)
class Place:
    latitude: float
    longitude: float
    how: Literal["visit", "path"]


def parse_geo(text: str) -> tuple[float, float]:
    if not text.startswith("geo:"):
        raise TimelineError(f"{text!r} is not a geo: location")
    latitude, longitude = text.removeprefix("geo:").split(",")
    return float(latitude), float(longitude)


type Visit = tuple[datetime, datetime, float, float]
type Point = tuple[datetime, float, float]


class Timeline:
    def __init__(self, visits: Sequence[Visit], points: Sequence[Point]) -> None:
        self.visits = sorted(visits, key=lambda v: v[1] - v[0])
        self.points = sorted(points)
        self._times = [at for at, _, _ in self.points]

    def locate(self, when: datetime, tolerance: timedelta) -> Place | None:
        """Where the phone was at ``when`` (which needs its UTC offset), if the export knows."""
        if when.tzinfo is None:
            raise ValueError("a moment without a time zone cannot be placed on the Timeline")
        for start, end, latitude, longitude in self.visits:
            if start <= when <= end:
                return Place(latitude, longitude, "visit")
        after = bisect.bisect_left(self._times, when)
        near = [
            point
            for point in (self.points[after - 1] if after else None,
                          self.points[after] if after < len(self.points) else None)
            if point is not None and abs(point[0] - when) <= tolerance
        ]  # fmt: skip
        if len(near) == 2 and near[0][0] != near[1][0]:
            (t0, lat0, lon0), (t1, lat1, lon1) = near
            share = (when - t0) / (t1 - t0)
            return Place(lat0 + (lat1 - lat0) * share, lon0 + (lon1 - lon0) * share, "path")
        if near:
            _, latitude, longitude = min(near, key=lambda p: abs(p[0] - when))
            return Place(latitude, longitude, "path")
        return None


def _segment(segment: Any, visits: list[Visit], points: list[Point]) -> None:
    start = datetime.fromisoformat(segment["startTime"])
    end = datetime.fromisoformat(segment["endTime"])
    if "visit" in segment:
        latitude, longitude = parse_geo(segment["visit"]["topCandidate"]["placeLocation"])
        visits.append((start, end, latitude, longitude))
    elif "activity" in segment:
        points.append((start, *parse_geo(segment["activity"]["start"])))
        points.append((end, *parse_geo(segment["activity"]["end"])))
    elif "timelinePath" in segment:
        for point in segment["timelinePath"]:
            minutes = float(point["durationMinutesOffsetFromStartTime"])
            points.append((start + timedelta(minutes=minutes), *parse_geo(point["point"])))


def parse_timeline(data: object) -> Timeline:
    if not isinstance(data, list):
        raise TimelineError("a Timeline export is a list of segments (the iOS format)")
    visits: list[Visit] = []
    points: list[Point] = []
    for index, segment in enumerate(data):
        try:
            _segment(segment, visits, points)
        except (KeyError, TypeError, ValueError) as exc:
            raise TimelineError(f"segment {index} cannot be read: {exc}") from None
    return Timeline(visits, points)


def load_timeline(path: Path) -> Timeline:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TimelineError(f"no Timeline export at {path}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TimelineError(f"{path} is not JSON: {exc}") from None
    return parse_timeline(data)
