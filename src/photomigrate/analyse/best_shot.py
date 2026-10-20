"""Best shot: group similar shots of one moment, rank them, keep the top two (CLA-84).

**Groups.** Shots are taken in time order. Each one joins the open group holding the shot it
is most like, when that similarity (cosine of open_clip embeddings) reaches
``min_similarity``; otherwise it starts a group. A group stays open for ``window_seconds``
after its first shot, so every pair in it is within the window, and a long series of the
same view becomes several groups rather than one. Times are compared as wall-clock time
(an offset, when there is one, is dropped), because shots of one moment come from one camera.

**Scores**, each from 0 to 1:

* ``sharpness``: the shot's sharpness over the sharpest in its group;
* ``exposure``: 1 at mid-grey, less with distance from it and with clipped pixels;
* ``faces``: the mean Vision capture quality of its faces (0 with no face);
* ``eyes_open``: the share of its faces with both eyes open (unknown counts half; 0 with no
  face);
* ``aesthetic``: Apple's overall aesthetic score (iCloud only).

A shot's score is their weighted mean (weights in the config file). A score some shot in the
group lacks is left out for the whole group, so shots are always compared on the same terms.
The face scores count only when every shot was read for faces (macOS) and one has a face.
Without Vision (Linux), groups are ranked on the other scores.

**Keep the top two** (Delmar's decision). Ties go to the higher resolution, then iCloud, then
the key, so the ranking is deterministic. The rest are *proposed* for "To Delete": the
review confirms each group.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from photomigrate.catalogue import Source
from photomigrate.config import BestShotWeights
from photomigrate.embedding import Vector
from photomigrate.faces import Face
from photomigrate.quality import PhotoMetrics

KEEP = 2
"""How many shots of each group are kept: Delmar's decision, not a setting."""

_MID_GREY = 128.0


@dataclass(frozen=True)
class Shot:
    id: int
    source: Source
    key: str
    taken: datetime
    embedding: Vector = field(compare=False)
    metrics: PhotoMetrics | None
    faces: tuple[Face, ...] | None
    """``None``: not read for faces; ``()``: read, none found."""
    aesthetic: float | None
    pixels: int

    @property
    def wall(self) -> datetime:
        return self.taken.replace(tzinfo=None)


# --- grouping ------------------------------------------------------------------------------


def group_shots(
    shots: Iterable[Shot], *, window: timedelta, min_similarity: float
) -> list[list[Shot]]:
    """Groups of two or more similar shots, each in time order, ordered by their first."""
    groups: list[list[Shot]] = []
    stacks: list[list[Vector]] = []
    open_from = 0  # groups before this index started more than ``window`` ago
    for current in sorted(shots, key=lambda s: (s.wall, s.source, s.key)):
        while open_from < len(groups) and current.wall - groups[open_from][0].wall > window:
            open_from += 1
        best, best_similarity = None, min_similarity
        for index in range(open_from, len(groups)):
            similarity = float(np.max(np.stack(stacks[index]) @ current.embedding))
            if similarity >= best_similarity and (best is None or similarity > best_similarity):
                best, best_similarity = index, similarity
        if best is None:
            groups.append([current])
            stacks.append([current.embedding])
        else:
            groups[best].append(current)
            stacks[best].append(current.embedding)
    return [group for group in groups if len(group) > 1]


# --- ranking -------------------------------------------------------------------------------


def exposure(metrics: PhotoMetrics) -> float:
    """1 at mid-grey; less with distance from it and with the share of clipped pixels."""
    distance = abs(metrics.brightness - _MID_GREY) / _MID_GREY
    return max(0.0, 1.0 - distance - metrics.dark_fraction - metrics.bright_fraction)


def _face_quality(faces: tuple[Face, ...]) -> float:
    return sum(f.quality for f in faces) / len(faces) if faces else 0.0


def _eyes_open(faces: tuple[Face, ...]) -> float:
    states = [1.0 if f.eyes_open else 0.5 if f.eyes_open is None else 0.0 for f in faces]
    return sum(states) / len(states) if states else 0.0


@dataclass(frozen=True)
class RankedShot:
    shot: Shot
    score: float
    parts: dict[str, float]


@dataclass(frozen=True)
class Ranking:
    shots: list[RankedShot]
    """Best first."""
    used: tuple[str, ...]
    """The scores that counted for this group."""

    @property
    def keep(self) -> list[RankedShot]:
        return self.shots[:KEEP]

    @property
    def rest(self) -> list[RankedShot]:
        return self.shots[KEEP:]


def _parts(shots: Sequence[Shot]) -> dict[str, Callable[[Shot], float]]:
    """The scores every shot in the group has, as functions of a shot."""
    parts: dict[str, Callable[[Shot], float]] = {}
    measured = [s.metrics for s in shots]
    if all(m is not None for m in measured):
        sharpest = max(m.sharpness for m in measured if m is not None)
        parts["sharpness"] = lambda s: (
            s.metrics.sharpness / sharpest if s.metrics and sharpest > 0 else 0.0
        )
        parts["exposure"] = lambda s: exposure(s.metrics) if s.metrics else 0.0
    if all(s.faces is not None for s in shots) and any(s.faces for s in shots):
        parts["faces"] = lambda s: _face_quality(s.faces or ())
        parts["eyes_open"] = lambda s: _eyes_open(s.faces or ())
    if all(s.aesthetic is not None for s in shots):
        parts["aesthetic"] = lambda s: min(max(s.aesthetic or 0.0, 0.0), 1.0)
    return parts


def rank(shots: Sequence[Shot], weights: BestShotWeights) -> Ranking:
    available = _parts(shots)
    used = tuple(
        name for name in BestShotWeights.names() if name in available and getattr(weights, name)
    )
    total = sum(getattr(weights, name) for name in used)
    ranked = []
    for current in shots:
        parts = {name: round(available[name](current), 4) for name in used}
        score = sum(getattr(weights, name) * parts[name] for name in used) / total if used else 0
        ranked.append(RankedShot(current, round(score, 4), parts))
    ranked.sort(key=lambda r: (-r.score, -r.shot.pixels, r.shot.source != "icloud", r.shot.key))
    return Ranking(ranked, used)
