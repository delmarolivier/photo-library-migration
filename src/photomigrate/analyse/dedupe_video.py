"""Video dedupe: exact copies and re-encoded or downscaled ones (CLA-81).

Two videos are the same clip when they have the same SHA-256 (**exact**), or when they are
**near**: durations within ±0.5 s, signatures (pHashes of 8 evenly sampled frames) at most
8 bits apart per frame on average, the same aspect ratio (ignoring rotation), and, if both
carry a capture time, the same moment. Live Photo motion clips are left to M1-06.

Keeper: the highest resolution, then the highest bitrate, then the richest metadata (date,
GPS, caption), then the copy already in iCloud. Proposals only, as for photos.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

from photomigrate.analyse.grouping import (
    DuplicateGroup,
    UnionFind,
    apply_proposal,
    proposal_actions,
    same_value_pairs,
    star_groups,
)
from photomigrate.catalogue import Catalogue, CatalogueItem, Source
from photomigrate.dates import same_moment
from photomigrate.hashing import signature_distance
from photomigrate.runner import Action

_MOTION_FLAGS = frozenset({"live-photo-motion", "unpaired-motion"})


@dataclass(frozen=True)
class VideoDedupeConfig:
    duration_tolerance: float = 0.5
    signature_max: float = 8.0
    aspect_tolerance: float = 0.02
    same_moment: timedelta = field(default=timedelta(0))


@dataclass(frozen=True)
class VideoRecord:
    id: int
    source: Source
    key: str
    sha256: str | None
    signature: tuple[str, ...]
    duration: float | None
    width: int | None
    height: int | None
    bitrate: int | None
    taken: datetime | None
    has_date: bool
    has_gps: bool
    has_caption: bool


def video_keeper_order(records: Iterable[VideoRecord]) -> list[VideoRecord]:
    """Best keeper first: resolution, bitrate, metadata, iCloud, then the key."""
    return sorted(
        records,
        key=lambda r: (
            -((r.width or 0) * (r.height or 0)),
            -(r.bitrate or 0),
            -(int(r.has_date) + int(r.has_gps) + int(r.has_caption)),
            r.source != "icloud",
            r.key,
        ),
    )


def _aspect(record: VideoRecord) -> float | None:
    if not record.width or not record.height:
        return None
    return max(record.width, record.height) / min(record.width, record.height)


def _verified(a: VideoRecord, b: VideoRecord, config: VideoDedupeConfig) -> bool:
    if signature_distance(a.signature, b.signature) > config.signature_max:
        return False
    ratio_a, ratio_b = _aspect(a), _aspect(b)
    if (
        ratio_a
        and ratio_b
        and abs(ratio_a - ratio_b) / max(ratio_a, ratio_b) > config.aspect_tolerance
    ):
        return False
    return not (a.taken and b.taken and not same_moment(a.taken, b.taken, config.same_moment))


def find_video_duplicates(
    records: Sequence[VideoRecord], config: VideoDedupeConfig
) -> list[DuplicateGroup]:
    groups = UnionFind(r.id for r in records)
    for first, second in same_value_pairs(records, "sha256"):
        groups.join(first, second, "exact")
    near: set[frozenset[int]] = set()
    timed = sorted(
        (r for r in records if r.signature and r.duration), key=lambda r: r.duration or 0
    )
    for i, a in enumerate(timed):
        for b in timed[i + 1 :]:
            if (b.duration or 0) - (a.duration or 0) > config.duration_tolerance:
                break
            if _verified(a, b, config):
                near.add(frozenset((a.id, b.id)))
                groups.join(a.id, b.id, "near")

    def edge(a: VideoRecord, b: VideoRecord) -> str | None:
        if a.sha256 and a.sha256 == b.sha256:
            return "exact"
        return "near" if frozenset((a.id, b.id)) in near else None

    return star_groups(groups.components(records), edge, video_keeper_order)  # type: ignore[arg-type]


def _record(catalogue: Catalogue, item: CatalogueItem) -> VideoRecord:
    found = catalogue.date_candidates(cast(int, item.id))
    planned = json.loads(item.metadata_plan).get("taken")
    return VideoRecord(
        id=cast(int, item.id),
        source=item.source,
        key=item.source_key,
        sha256=item.sha256,
        signature=tuple(item.video_signature.split(",")) if item.video_signature else (),
        duration=item.duration,
        width=item.width,
        height=item.height,
        bitrate=item.bitrate,
        taken=found.get("quicktime"),
        has_date=bool(found.get("quicktime") or found.get("icloud") or planned),
        has_gps=item.latitude is not None,
        has_caption=bool(item.description),
    )


class VideoDedupeStage:
    """Propose video duplicate groups (``duplicate`` groups keyed ``vdup-…``)."""

    name = "dedupe-videos"

    def __init__(self, config: VideoDedupeConfig | None = None) -> None:
        self.config = config or VideoDedupeConfig()

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        ours = catalogue.own_imports()
        records = [
            _record(catalogue, item)
            for item in catalogue.items()
            if item.kind == "video"
            and item.id not in ours
            and (item.sha256 or item.video_signature)
            and not (_MOTION_FLAGS & item.flags)
        ]
        found = find_video_duplicates(records, self.config)
        yield from proposal_actions(catalogue, found, {r.id: r for r in records}, "vdup-")

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        apply_proposal(action, catalogue)
