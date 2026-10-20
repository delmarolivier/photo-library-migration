"""Synthetic Google Maps Timeline exports in the iOS on-device format (see CLA-87).

The real export is a JSON list of segments with ``startTime`` and ``endTime`` and one of
``visit``, ``activity`` or ``timelinePath``; every value is a string, places are
``geo:lat,lon``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

type Point = tuple[float, float]


def _time(when: datetime) -> str:
    return when.isoformat(timespec="milliseconds")


def _geo(latitude: float, longitude: float) -> str:
    return f"geo:{latitude:.6f},{longitude:.6f}"


class TimelineBuilder:
    def __init__(self) -> None:
        self.segments: list[dict[str, object]] = []

    def _segment(self, start: datetime, end: datetime, **body: object) -> TimelineBuilder:
        self.segments.append({"startTime": _time(start), "endTime": _time(end), **body})
        return self

    def visit(
        self, start: datetime, end: datetime, latitude: float, longitude: float
    ) -> TimelineBuilder:
        return self._segment(
            start, end,
            visit={
                "hierarchyLevel": "0", "probability": "0.9",
                "topCandidate": {
                    "placeID": "ChIJ-synthetic", "semanticType": "Unknown", "probability": "0.8",
                    "placeLocation": _geo(latitude, longitude),
                },
            },
        )  # fmt: skip

    def activity(
        self, start: datetime, end: datetime, origin: Point, destination: Point
    ) -> TimelineBuilder:
        return self._segment(
            start, end,
            activity={
                "start": _geo(*origin), "end": _geo(*destination), "distanceMeters": "1000.0",
                "probability": "0.9", "topCandidate": {"type": "walking", "probability": "0.8"},
            },
        )  # fmt: skip

    def path(
        self, start: datetime, end: datetime, points: Sequence[tuple[float, float, float]]
    ) -> TimelineBuilder:
        """``points``: (minutes after ``start``, latitude, longitude)."""
        return self._segment(
            start, end,
            timelinePath=[
                {"point": _geo(lat, lon), "durationMinutesOffsetFromStartTime": f"{minutes:g}"}
                for minutes, lat, lon in points
            ],
        )  # fmt: skip

    def data(self) -> list[dict[str, object]]:
        return json.loads(self.to_json())  # type: ignore[no-any-return]

    def to_json(self) -> str:
        return json.dumps(self.segments, indent=2)
