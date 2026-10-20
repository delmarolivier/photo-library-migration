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

**The stage** takes every photo with an embedding and a capture time (EXIF, else the Photos
date, else the date ingest plans to write). Left out: Google edits and creations (as in
dedupe), and photos proposed as another's duplicate (dedupe keeps one copy; best-shot ranks
that one). Groups are ``best-shot`` groups with member roles ``keep`` and ``to-delete`` in rank
order, each Live Photo still followed by its ``motion`` clip. Their details hold the scores
used, each shot's score and parts, and the two kept. Stale groups are retired.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

import numpy as np

from photomigrate.catalogue import Catalogue, CatalogueItem, Source
from photomigrate.config import BestShotConfig, BestShotWeights
from photomigrate.embedding import Vector, from_bytes
from photomigrate.faces import Face, faces_from_json
from photomigrate.quality import PhotoMetrics
from photomigrate.runner import Action, JSONValue

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


# --- the stage -----------------------------------------------------------------------------

_LEFT_OUT = frozenset({"edited", "google-creation"})


def _taken(catalogue: Catalogue, item: CatalogueItem) -> datetime | None:
    found = catalogue.date_candidates(cast(int, item.id))
    for source in ("exif", "icloud"):
        if source in found:
            return found[source]
    planned = json.loads(item.metadata_plan).get("taken")
    return datetime.fromisoformat(planned) if planned else None


def _shot(item: CatalogueItem, taken: datetime) -> Shot:
    measured = json.loads(item.quality)
    faces = faces_from_json(item.faces)
    vector = from_bytes(cast(bytes, item.embedding))
    norm = float(np.linalg.norm(vector))
    return Shot(
        id=cast(int, item.id),
        source=item.source,
        key=item.source_key,
        taken=taken,
        embedding=vector / norm if norm else vector,
        metrics=PhotoMetrics.from_dict(measured) if measured else None,
        faces=None if faces is None else tuple(faces),
        aesthetic=json.loads(item.scores).get("overall"),
        pixels=(item.width or 0) * (item.height or 0),
    )


def _name(source: Source, key: str) -> str:
    return f"{source}:{key}"


class BestShotStage:
    """Propose ``best-shot`` groups: the top two kept, the rest for "To Delete"."""

    name = "best-shot"

    def __init__(self, config: BestShotConfig) -> None:
        self.config = config

    def _shots(self, catalogue: Catalogue) -> list[Shot]:
        duplicates = {
            item_id
            for group in catalogue.groups("duplicate")
            for item_id, role, _ in catalogue.group_members(group.id)
            if role != "keeper"
        }
        shots = []
        for item in catalogue.items():
            if (
                item.kind != "photo"
                or item.embedding is None
                or item.id in duplicates
                or (item.source == "google" and _LEFT_OUT & item.flags)
            ):
                continue
            taken = _taken(catalogue, item)
            if taken is not None:
                shots.append(_shot(item, taken))
        return shots

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        motions: dict[int, CatalogueItem] = {}
        for pair in catalogue.groups("live-pair"):
            roles = {role: item_id for item_id, role, _ in catalogue.group_members(pair.id)}
            motions[roles["still"]] = catalogue.item(roles["motion"])
        found = group_shots(
            self._shots(catalogue),
            window=timedelta(seconds=self.config.window_seconds),
            min_similarity=self.config.min_similarity,
        )
        wanted = set()
        for group in found:
            ranking = rank(group, self.config.weights)
            names = sorted(_name(s.source, s.key) for s in group)
            key = "best-" + hashlib.sha256("|".join(names).encode()).hexdigest()[:16]
            wanted.add(key)
            members: list[JSONValue] = []
            for position, ranked in enumerate(ranking.shots):
                shot = ranked.shot
                members.append([shot.source, shot.key, "keep" if position < KEEP else "to-delete"])
                if shot.id in motions:
                    motion = motions[shot.id]
                    members.append([motion.source, motion.source_key, "motion"])
            details: dict[str, JSONValue] = {
                "used": list(ranking.used),
                "keep": [_name(r.shot.source, r.shot.key) for r in ranking.keep],
                "scores": {_name(r.shot.source, r.shot.key): r.score for r in ranking.shots},
                "parts": {
                    _name(r.shot.source, r.shot.key): cast(dict[str, JSONValue], r.parts)
                    for r in ranking.shots
                },
                "members": members,
            }
            yield Action("propose-best-shot", key, details)
        for stale in catalogue.groups("best-shot"):
            if stale.key not in wanted:
                yield Action("retire-best-shot", stale.key)

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        if action.kind == "retire-best-shot":
            catalogue.discard_group("best-shot", action.target)
            return
        details = dict(action.details)
        listed = cast(list[list[str]], details.pop("members"))
        group = catalogue.upsert_group("best-shot", action.target, details)
        members = []
        for rank_, (source, key, role) in enumerate(listed):
            item_id = catalogue.item_for_key(cast(Source, source), key)
            if item_id is None:
                raise KeyError(f"{source}:{key} is no longer in the catalogue")
            members.append((item_id, role, rank_))
        catalogue.set_group_members(group, members)
