"""Re-pairing Live Photos that Takeout split into a still and a short motion clip.

Matching rules, in order:

1. **Content identifier**: Apple's shared ID (MakerNotes on the still, a QuickTime key on the
   clip). It works even if the files were renamed.
2. **Name and time**: the same folder and the same name stem (including any ``(N)``), with
   capture times at most 3 seconds apart (allowing for offset-less EXIF). If either time is
   unknown, the name alone decides.

Only clips of 3.5 seconds or less can be motion halves. A short clip that carries an
identifier, or has a same-named still, but finds no partner is *unpaired* and goes to
review. Other short clips are just videos.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Literal, cast

from photomigrate.catalogue import Catalogue, CatalogueItem, DateSource
from photomigrate.dates import moment_gap
from photomigrate.runner import Action, JSONValue

MAX_MOTION_SECONDS = 3.5
SAME_SHOT = timedelta(seconds=3)

PairMethod = Literal["content-id", "name-time", "name"]


@dataclass(frozen=True)
class Candidate:
    key: str
    """The item's Takeout path (its catalogue ``source_key``)."""
    time: datetime | None
    content_id: str | None
    duration: float | None


@dataclass(frozen=True)
class Pair:
    still: str
    motion: str
    method: PairMethod
    delta: float | None
    """Seconds between the two capture times, when both are known."""


@dataclass(frozen=True)
class Pairing:
    pairs: tuple[Pair, ...]
    unpaired: tuple[str, ...]


def _name(key: str) -> tuple[str, str]:
    path = PurePosixPath(key)
    return str(path.parent), path.stem.casefold()


def _gap(still: Candidate, motion: Candidate) -> timedelta | None:
    if still.time is None or motion.time is None:
        return None
    return moment_gap(still.time, motion.time)


def _seconds(gap: timedelta | None) -> float | None:
    return None if gap is None else gap.total_seconds()


def pair_live_photos(stills: list[Candidate], motions: list[Candidate]) -> Pairing:
    short = sorted(
        (m for m in motions if m.duration is None or m.duration <= MAX_MOTION_SECONDS),
        key=lambda m: m.key,
    )
    by_id = {s.content_id: s for s in sorted(stills, key=lambda s: s.key) if s.content_id}
    by_name: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    for still in sorted(stills, key=lambda s: s.key):
        by_name[_name(still.key)].append(still)

    pairs: list[Pair] = []
    used: set[str] = set()
    paired: set[str] = set()
    for motion in short:
        twin = by_id.get(motion.content_id) if motion.content_id else None
        if twin is not None and twin.key not in used:
            pairs.append(Pair(twin.key, motion.key, "content-id", _seconds(_gap(twin, motion))))
            used.add(twin.key)
            paired.add(motion.key)

    options = []
    for motion in short:
        if motion.key in paired:
            continue
        for still in by_name.get(_name(motion.key), []):
            gap = _gap(still, motion)
            if gap is None or gap <= SAME_SHOT:
                rank = gap if gap is not None else timedelta.max
                options.append((rank, motion.key, still.key, gap))
    for _, motion_key, still_key, gap in sorted(options):
        if motion_key not in paired and still_key not in used:
            method: PairMethod = "name" if gap is None else "name-time"
            pairs.append(Pair(still_key, motion_key, method, _seconds(gap)))
            used.add(still_key)
            paired.add(motion_key)

    unpaired = tuple(
        m.key for m in short if m.key not in paired and (m.content_id or _name(m.key) in by_name)
    )
    return Pairing(tuple(sorted(pairs, key=lambda p: p.still)), unpaired)


STILL_EXTENSIONS = frozenset({".heic", ".heif", ".jpg", ".jpeg"})
MOTION_EXTENSIONS = frozenset({".mov", ".mp4"})


def _capture_time(
    catalogue: Catalogue, item: CatalogueItem, embedded: DateSource
) -> datetime | None:
    found = catalogue.date_candidates(cast(int, item.id))
    return found.get(embedded) or found.get("sidecar-taken")


class LivePhotoStage:
    """Pair catalogued Google stills and motion clips into ``live-pair`` groups."""

    name = "live-photos"

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        stills, motions = [], []
        for item in catalogue.items("google"):
            suffix = PurePosixPath(item.source_key).suffix.lower()
            if item.kind == "photo" and suffix in STILL_EXTENSIONS:
                if not {"edited", "google-creation"} & item.flags:
                    time = _capture_time(catalogue, item, "exif")
                    stills.append(Candidate(item.source_key, time, item.content_id, None))
            elif item.kind == "video" and suffix in MOTION_EXTENSIONS:
                time = _capture_time(catalogue, item, "quicktime")
                motions.append(Candidate(item.source_key, time, item.content_id, item.duration))
        result = pair_live_photos(stills, motions)
        for pair in result.pairs:
            details: dict[str, JSONValue] = {
                "motion": pair.motion,
                "method": pair.method,
                "delta": pair.delta,
            }
            yield Action("pair-live", pair.still, details)
        for key in result.unpaired:
            yield Action("flag-unpaired-motion", key)

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        if action.kind == "flag-unpaired-motion":
            catalogue.add_flags(self._id(catalogue, action.target), {"unpaired-motion"})
            return
        still = self._id(catalogue, action.target)
        motion = self._id(catalogue, str(action.details["motion"]))
        group = catalogue.upsert_group("live-pair", action.target, dict(action.details))
        catalogue.set_group_members(group, [(still, "still", 0), (motion, "motion", 1)])
        catalogue.add_flags(still, {"live-photo-still"})
        catalogue.add_flags(motion, {"live-photo-motion"})

    @staticmethod
    def _id(catalogue: Catalogue, key: str) -> int:
        found = catalogue.item_for_key("google", key)
        if found is None:
            raise KeyError(f"{key} is not in the catalogue; run ingest first")
        return found
