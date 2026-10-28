"""Highlights: keep the best share of each year's photos and videos, spread across its events.

Delmar's decisions (2026-10-28): after duplicates, best-shot losers and clutter, keep only the
best 20% of each year's photos (``highlights.share``), and the same of its videos, cut apart.
Favourites are always kept. Album photos are not: one album holds 29,068 photos.

**Events.** A year's photos (or videos) are taken in time order and split wherever two are
more than ``event_gap_hours`` apart, so a trip, a party or an afternoon is one event.

**The quota** is the share of the year's photos (or videos), rounded up. Favourites are kept
whatever the quota, and count towards it. The places left are shared across the events in
proportion to their other items (largest remainders; a small event may get none).

**Photos** in an event are ranked as best shot ranks a group (sharpness against the event's
sharpest, exposure, faces, eyes open, Apple's aesthetic score where every photo has one),
blended with ``appeal``: open_clip's judgement of how good a photo it is (``appeal_weight``).
The top ones are taken in turn, skipping a photo at least ``max_similarity`` like one already
taken, unless nothing else is left.

**Videos** have no embedding: they are ranked on resolution, length, exposure and motion
(``video_score``). A Live Photo's clip is no video of its own: it follows its still.

**The stage** proposes one ``highlights`` group per event that sets something aside (photos
and videos apart, ``kind`` in the details), with member roles ``keep`` and ``to-delete`` (each
Live Photo followed by its ``motion`` clip). The review confirms or changes the picks, as for
best shot.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

import numpy as np

from photomigrate.analyse.best_shot import Shot, rank, shot_of, taken_of
from photomigrate.catalogue import Catalogue, CatalogueItem, Source
from photomigrate.config import BestShotWeights, HighlightsConfig
from photomigrate.embedding import Embedder, Vector
from photomigrate.runner import Action, JSONValue

Protection = str
"""Why a photo is always kept: ``favourite``."""


@dataclass(frozen=True)
class Candidate:
    shot: Shot
    protected: Protection | None = None


@dataclass(frozen=True)
class Event:
    shots: list[Shot]
    """In time order."""
    keep: list[int]
    """The ids kept, best first (protected ones first of all)."""
    scores: dict[int, float] = field(default_factory=dict)
    protected: dict[int, Protection] = field(default_factory=dict)


# --- appeal --------------------------------------------------------------------------------

GOOD = (
    "a beautiful, memorable photograph",
    "a well composed photo of a special moment",
    "a stunning travel photo",
    "a lovely portrait of smiling people",
)
BAD = (
    "a dull, ordinary snapshot",
    "a blurry, badly framed photo",
    "a boring photo of nothing in particular",
    "a dark, out of focus picture",
)
_LOGIT_SCALE = 100.0
"""open_clip's learned temperature, as in clutter's zero-shot labels."""


class Appeal:
    """How much a photo looks like a good one rather than a poor one, from 0 to 1: open_clip's
    zero-shot share of the good prompts, from the photo's stored embedding."""

    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder
        self._prompts: np.ndarray | None = None

    def score(self, vector: Vector) -> float:
        if self._prompts is None:
            self._prompts = np.stack(self.embedder.embed_texts([*GOOD, *BAD]))
        logits = (self._prompts @ vector) * _LOGIT_SCALE
        weights = np.exp(logits - logits.max())
        return round(float(weights[: len(GOOD)].sum() / weights.sum()), 4)


# --- quota and allocation --------------------------------------------------------------------


def quota(count: int, share: float) -> int:
    """The share of ``count``, rounded up (``round`` first: 30 x 0.1 is not quite 3)."""
    return math.ceil(round(count * share, 9))


def allocate(sizes: Sequence[int], slots: int) -> list[int]:
    """``slots`` places shared in proportion to ``sizes`` by largest remainders, each capped
    at its size. Ties go to the earlier event."""
    total = sum(sizes)
    slots = min(slots, total)
    if not slots:
        return [0] * len(sizes)
    ideal = [slots * size / total for size in sizes]
    given = [min(size, math.floor(share)) for size, share in zip(sizes, ideal, strict=True)]
    order = sorted(range(len(sizes)), key=lambda i: (-(ideal[i] - math.floor(ideal[i])), i))
    while sum(given) < slots:
        for i in order:
            if sum(given) < slots and given[i] < sizes[i]:
                given[i] += 1
    return given


# --- selection -------------------------------------------------------------------------------


def events(shots: Sequence[Shot], gap: timedelta) -> list[list[Shot]]:
    """Shots in time order, split wherever two are more than ``gap`` apart."""
    found: list[list[Shot]] = []
    for current in sorted(shots, key=lambda s: (s.wall, s.source, s.key)):
        if found and current.wall - found[-1][-1].wall <= gap:
            found[-1].append(current)
        else:
            found.append([current])
    return found


def _scores(
    shots: Sequence[Shot],
    weights: BestShotWeights,
    appeal: Callable[[Shot], float] | None,
    appeal_weight: float,
) -> dict[int, float]:
    ranking = rank(shots, weights)
    if appeal is None:
        return {r.shot.id: r.score for r in ranking.shots}
    return {
        r.shot.id: round((1 - appeal_weight) * r.score + appeal_weight * appeal(r.shot), 4)
        for r in ranking.shots
    }


def _pick(ranked: Sequence[Shot], slots: int, max_similarity: float) -> list[Shot]:
    """The top ``slots`` shots, passing over near repeats of those taken while others remain."""
    taken: list[Shot] = []
    passed: list[Shot] = []
    for current in ranked:
        if len(taken) == slots:
            break
        if any(float(current.embedding @ t.embedding) >= max_similarity for t in taken):
            passed.append(current)
        else:
            taken.append(current)
    return taken + passed[: slots - len(taken)]


def photo_scores(
    weights: BestShotWeights, *, appeal: Callable[[Shot], float] | None, appeal_weight: float
) -> Callable[[Sequence[Shot]], dict[int, float]]:
    """An event's photos scored as best shot ranks a group, blended with their appeal."""
    return lambda shots: _scores(shots, weights, appeal, appeal_weight)


_FULL_HD = 1920 * 1080
_VIDEO_WEIGHTS = (0.3, 0.3, 0.2, 0.2)


def video_score(
    *,
    width: int | None,
    height: int | None,
    duration: float | None,
    brightness: float | None,
    motion: float | None,
) -> float:
    """A video's score from 0 to 1, from what ingest measured (videos have no embedding):
    resolution against Full HD, length (best from 10 seconds to 3 minutes), exposure (1 at
    mid-grey) and motion (something happens). A missing measurement counts 0."""
    if duration is None:
        length = 0.0
    elif duration < 10:
        length = duration / 10
    else:
        length = min(1.0, 180 / duration)
    parts = (
        min(1.0, width * height / _FULL_HD) if width and height else 0.0,
        length,
        max(0.0, 1 - abs(brightness - 128) / 128) if brightness is not None else 0.0,
        min(1.0, motion / 10) if motion is not None else 0.0,
    )
    return round(sum(w * p for w, p in zip(_VIDEO_WEIGHTS, parts, strict=True)), 4)


def select_year(
    candidates: Sequence[Candidate],
    *,
    share: float,
    gap: timedelta,
    score: Callable[[Sequence[Shot]], dict[int, float]],
    max_similarity: float,
) -> list[Event]:
    """One year's events, each with what it keeps. ``score`` scores an event's shots."""
    protected = {c.shot.id: c.protected for c in candidates if c.protected}
    grouped = events([c.shot for c in candidates], gap)
    free = [[s for s in shots if s.id not in protected] for shots in grouped]
    slots = max(0, quota(len(candidates), share) - len(protected))
    places = allocate([len(f) for f in free], slots)
    found = []
    for shots, others, count in zip(grouped, free, places, strict=True):
        scores = score(shots)
        ranked = sorted(others, key=lambda s: (-scores[s.id], -s.pixels, s.key))
        kept = [s.id for s in shots if s.id in protected]
        kept += [s.id for s in _pick(ranked, count, max_similarity)]
        found.append(
            Event(
                shots=shots,
                keep=kept,
                scores=scores,
                protected={s.id: protected[s.id] for s in shots if s.id in protected},
            )
        )
    return found


# --- the stage -----------------------------------------------------------------------------

_LEFT_OUT = frozenset({"edited", "google-creation"})


def _name(shot: Shot) -> str:
    return f"{shot.source}:{shot.key}"


def _video_taken(catalogue: Catalogue, item: CatalogueItem) -> datetime | None:
    found = catalogue.date_candidates(cast(int, item.id))
    for source in ("quicktime", "icloud"):
        if source in found:
            return found[source]
    planned = json.loads(item.metadata_plan).get("taken")
    return datetime.fromisoformat(planned) if planned else None


def _video_shot(item: CatalogueItem, taken: datetime) -> Shot:
    """A video as a shot: its score kept in ``aesthetic``, with no embedding to compare."""
    measured = json.loads(item.quality)
    score = video_score(
        width=item.width,
        height=item.height,
        duration=item.duration,
        brightness=measured.get("brightness"),
        motion=measured.get("motion"),
    )
    return Shot(
        id=cast(int, item.id),
        source=item.source,
        key=item.source_key,
        taken=taken,
        embedding=np.zeros(1, dtype=np.float32),
        metrics=None,
        faces=None,
        aesthetic=score,
        pixels=(item.width or 0) * (item.height or 0),
    )


def _video_scores(shots: Sequence[Shot]) -> dict[int, float]:
    return {s.id: s.aesthetic or 0.0 for s in shots}


class HighlightsStage:
    """Propose ``highlights`` groups: each year's best share kept, the rest for "To Delete"."""

    name = "highlights"

    def __init__(
        self, config: HighlightsConfig, weights: BestShotWeights, *, appeal: Appeal | None
    ) -> None:
        self.config = config
        self.weights = weights
        self.appeal = appeal

    def _set_aside(self, catalogue: Catalogue) -> set[int]:
        """Photos another proposal already sets aside: copies, best-shot losers, clutter."""
        found = set(catalogue.clutter())
        for kind, role in (("duplicate", "duplicate"), ("best-shot", "to-delete")):
            for group in catalogue.groups(kind):  # type: ignore[arg-type]
                found |= {i for i, r, _ in catalogue.group_members(group.id) if r == role}
        return found | catalogue.own_imports()

    def _candidates(self, catalogue: Catalogue) -> dict[tuple[str, int], list[Candidate]]:
        """Each year's photos and, apart, its videos (not Live Photo clips: they follow)."""
        set_aside = self._set_aside(catalogue)
        motions = {
            i
            for pair in catalogue.groups("live-pair")
            for i, role, _ in catalogue.group_members(pair.id)
            if role == "motion"
        }
        found: dict[tuple[str, int], list[Candidate]] = defaultdict(list)
        for item in catalogue.items():
            if (
                item.id in set_aside
                or item.id in motions
                or (item.source == "google" and _LEFT_OUT & item.flags)
                or (item.kind == "photo" and item.embedding is None)
            ):
                continue
            taken = (
                taken_of(catalogue, item) if item.kind == "photo" else _video_taken(catalogue, item)
            )
            if taken is None:
                continue
            shot = shot_of(item, taken) if item.kind == "photo" else _video_shot(item, taken)
            protected = "favourite" if item.favorite else None
            found[(item.kind, taken.year)].append(Candidate(shot, protected))
        return found

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        motions: dict[int, CatalogueItem] = {}
        for pair in catalogue.groups("live-pair"):
            roles = {role: item_id for item_id, role, _ in catalogue.group_members(pair.id)}
            motions[roles["still"]] = catalogue.item(roles["motion"])
        appeal = self.appeal.score if self.appeal else None
        photos = photo_scores(
            self.weights,
            appeal=(lambda s: appeal(s.embedding)) if appeal else None,
            appeal_weight=self.config.appeal_weight,
        )
        scorers = {"photo": (photos, self.config.max_similarity), "video": (_video_scores, 1.0)}
        wanted = set()
        for (kind, year), candidates in sorted(self._candidates(catalogue).items()):
            score, max_similarity = scorers[kind]
            found = select_year(
                candidates,
                share=self.config.share,
                gap=timedelta(hours=self.config.event_gap_hours),
                score=score,
                max_similarity=max_similarity,
            )
            for event in found:
                if len(event.keep) == len(event.shots):
                    continue  # nothing set aside: nothing to ask
                names = sorted(_name(s) for s in event.shots)
                key = "hl-" + hashlib.sha256("|".join(names).encode()).hexdigest()[:16]
                wanted.add(key)
                details = self._details(year, event, motions)
                yield Action("propose-highlights", key, {"kind": kind, **details})
        for stale in catalogue.groups("highlights"):
            if stale.key not in wanted:
                yield Action("retire-highlights", stale.key)

    def _details(
        self, year: int, event: Event, motions: dict[int, CatalogueItem]
    ) -> dict[str, JSONValue]:
        kept = set(event.keep)
        by_id = {s.id: s for s in event.shots}
        members: list[JSONValue] = []
        for shot in sorted(event.shots, key=lambda s: (s.id not in kept, -event.scores[s.id])):
            members.append([shot.source, shot.key, "keep" if shot.id in kept else "to-delete"])
            if shot.id in motions:
                motion = motions[shot.id]
                members.append([motion.source, motion.source_key, "motion"])
        return {
            "year": year,
            "share": self.config.share,
            "start": event.shots[0].taken.isoformat(),
            "end": event.shots[-1].taken.isoformat(),
            "keep": [_name(by_id[i]) for i in event.keep],
            "protected": {_name(by_id[i]): why for i, why in event.protected.items()},
            "scores": {_name(s): event.scores[s.id] for s in event.shots},
            "members": members,
        }

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        if action.kind == "retire-highlights":
            catalogue.discard_group("highlights", action.target)
            return
        details = dict(action.details)
        listed = cast(list[list[str]], details.pop("members"))
        group = catalogue.upsert_group("highlights", action.target, details)
        members = []
        for rank_, (source, key, role) in enumerate(listed):
            item_id = catalogue.item_for_key(cast(Source, source), key)
            if item_id is None:
                raise KeyError(f"{source}:{key} is no longer in the catalogue")
            members.append((item_id, role, rank_))
        catalogue.set_group_members(group, members)
