"""Location backfill: infer where a shot without GPS was taken (CLA-87).

* **Neighbours:** the same camera's located shots within ``window_minutes`` (30) before and
  after it, by wall-clock time. Between two, the location is interpolated in time; with one,
  it is copied. The nearest shot on each side counts. A shot without a camera has no
  neighbours: the same device is the point.
* **Timeline**, when ``analyse --timeline`` gives a Google Maps Timeline export and no
  neighbour helped: where the phone was at that moment (``timeline.Timeline.locate``). Local
  times without an offset are read in the home zone.

Inferred locations are **proposals** (catalogue v13 ``inferred_locations``: place, method,
evidence). Nothing is written until the review approves one (the item's ``location``
decision, ``{"approve": true}``). The importer then writes the GPS with the keyword
``location-inferred`` (``approved_location``).

Left out: items with their own GPS, Google edits, creations and Live Photo motion clips (they
follow their original or still), proposed duplicates, and the clutter queue.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Literal, cast

from photomigrate.analyse.date_audit import dated_items
from photomigrate.catalogue import Catalogue, Source
from photomigrate.config import LocationConfig
from photomigrate.runner import Action, JSONValue
from photomigrate.timeline import Timeline

Method = Literal["neighbour", "interpolated", "timeline-visit", "timeline-path"]
INFERRED_KEYWORD = "location-inferred"


@dataclass(frozen=True)
class Spot:
    id: int
    key: str
    camera: str | None
    taken: datetime | None
    latitude: float | None
    longitude: float | None

    @property
    def wall(self) -> datetime:
        return cast(datetime, self.taken).replace(tzinfo=None)


@dataclass(frozen=True)
class Inferred:
    latitude: float
    longitude: float
    method: Method
    evidence: dict[str, list[str] | list[int]] = field(default_factory=dict)


def _seconds(gap: timedelta) -> int:
    return round(abs(gap.total_seconds()))


def _from_neighbours(current: Spot, located: Sequence[Spot], window: timedelta) -> Inferred | None:
    times = [s.wall for s in located]
    after = bisect.bisect_left(times, current.wall)
    sides = [
        located[after - 1] if after else None,
        located[after] if after < len(located) else None,
    ]
    near = [s for s in sides if s is not None and abs(s.wall - current.wall) <= window]
    if not near:
        return None
    evidence: dict[str, list[str] | list[int]] = {
        "from": [s.key for s in near],
        "gaps": [_seconds(s.wall - current.wall) for s in near],
    }
    if len(near) == 2 and near[0].wall != near[1].wall:
        first, last = near
        share = (current.wall - first.wall) / (last.wall - first.wall)
        lat0, lon0 = cast(float, first.latitude), cast(float, first.longitude)
        lat1, lon1 = cast(float, last.latitude), cast(float, last.longitude)
        return Inferred(
            lat0 + (lat1 - lat0) * share, lon0 + (lon1 - lon0) * share, "interpolated", evidence
        )
    nearest = min(near, key=lambda s: abs(s.wall - current.wall))
    evidence = {"from": [nearest.key], "gaps": [_seconds(nearest.wall - current.wall)]}
    return Inferred(
        cast(float, nearest.latitude), cast(float, nearest.longitude), "neighbour", evidence
    )


def infer(
    spots: Sequence[Spot],
    *,
    window: timedelta,
    timeline: Timeline | None = None,
    zone: tzinfo | None = None,
    tolerance: timedelta = timedelta(minutes=15),
) -> dict[int, Inferred]:
    """A proposed location for every shot without one that its neighbours or the Timeline
    can place."""
    by_camera: dict[str, list[Spot]] = defaultdict(list)
    for current in spots:
        if current.camera and current.taken and current.latitude is not None:
            by_camera[current.camera].append(current)
    for located in by_camera.values():
        located.sort(key=lambda s: (s.wall, s.key))
    found: dict[int, Inferred] = {}
    for current in spots:
        if current.latitude is not None or not current.camera or current.taken is None:
            continue
        guess = _from_neighbours(current, by_camera.get(current.camera, []), window)
        if guess is None and timeline is not None:
            moment = current.taken
            if moment.tzinfo is None:
                if zone is None:
                    raise ValueError("local times need the home zone to be placed on a Timeline")
                moment = moment.replace(tzinfo=zone)
            place = timeline.locate(moment, tolerance)
            if place is not None:
                method: Method = "timeline-visit" if place.how == "visit" else "timeline-path"
                guess = Inferred(place.latitude, place.longitude, method)
        if guess is not None:
            found[current.id] = guess
    return found


# --- the stage -----------------------------------------------------------------------------


def approved_location(catalogue: Catalogue, item_id: int) -> tuple[float, float] | None:
    """The proposed location the review approved for the item, for the importer to write
    (with the keyword ``location-inferred``), or ``None``."""
    answer = catalogue.decision("item", item_id, "location")
    if not answer or answer.get("approve") is not True:
        return None
    proposal = catalogue.inferred_locations().get(item_id)
    return None if proposal is None else proposal[0]


class LocationStage:
    """Propose locations for shots without GPS (``inferred_locations``); never write them."""

    name = "locations"

    def __init__(
        self,
        config: LocationConfig,
        timeline: Timeline | None = None,
        zone: tzinfo | None = None,
    ) -> None:
        self.config = config
        self.timeline = timeline
        self.zone = zone

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        queued = set(catalogue.clutter())
        items = {cast(int, item.id): item for item in catalogue.items()}
        spots = [
            Spot(
                id=d.id,
                key=d.key,
                camera=d.camera,
                taken=d.taken,
                latitude=items[d.id].latitude,
                longitude=items[d.id].longitude,
            )
            for d in dated_items(catalogue)
            if d.id not in queued
        ]
        found = infer(
            spots,
            window=timedelta(minutes=self.config.window_minutes),
            timeline=self.timeline,
            zone=self.zone,
            tolerance=timedelta(minutes=self.config.timeline_tolerance_minutes),
        )
        stored = catalogue.inferred_locations()
        for item_id, guess in found.items():
            place = (round(guess.latitude, 6), round(guess.longitude, 6))
            if stored.get(item_id) != (place, guess.method, guess.evidence):
                item = items[item_id]
                details: dict[str, JSONValue] = {
                    "source": item.source,
                    "latitude": place[0],
                    "longitude": place[1],
                    "method": guess.method,
                    "evidence": cast(dict[str, JSONValue], guess.evidence),
                }
                yield Action("propose-location", item.source_key, details)
        for item_id in sorted(stored.keys() - found.keys()):
            item = items[item_id]
            yield Action("retire-location", item.source_key, {"source": item.source})

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        source = cast(Source, action.details["source"])
        item_id = catalogue.item_for_key(source, action.target)
        if item_id is None:
            raise KeyError(f"{source}:{action.target} is no longer in the catalogue")
        if action.kind == "retire-location":
            catalogue.set_inferred_location(item_id, None)
            return
        details = action.details
        catalogue.set_inferred_location(
            item_id,
            (cast(float, details["latitude"]), cast(float, details["longitude"])),
            str(details["method"]),
            cast(dict[str, JSONValue], details["evidence"]),
        )
