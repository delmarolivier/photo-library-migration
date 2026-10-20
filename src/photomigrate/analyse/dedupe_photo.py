"""Photo dedupe: exact copies and near-duplicates, across and within sources (CLA-80).

Two photos are the same picture when one of these holds:

* **exact**: same SHA-256;
* **content-id**: same Apple Live Photo identifier;
* **near**: the 64-bit pHashes are within ``candidate_phash`` bits (a fast numpy search over
  all photos), *and* the 256-bit pHash (``fine``) and the dHash agree, *and* the aspect ratio
  matches (ignoring rotation), *and*, if both carry a camera time, it is the same moment.
  The fine hash keeps crops apart, and the time check keeps burst and "a moment later" shots
  apart; those belong to best-shot (M3-02).

Matches are merged into groups. Each group names a proposed keeper: the highest resolution,
then the richest metadata (date, GPS, caption), then the copy already in iCloud. Nothing is
decided here; the user confirms in review.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

import numpy as np

from photomigrate.analyse.grouping import (
    DuplicateGroup,
    UnionFind,
    apply_proposal,
    proposal_actions,
    same_value_pairs,
)
from photomigrate.catalogue import Catalogue, CatalogueItem, Source
from photomigrate.dates import same_moment
from photomigrate.hashing import hamming
from photomigrate.runner import Action

__all__ = ["DedupeConfig", "DedupeStage", "DuplicateGroup", "PhotoRecord", "find_duplicates"]


@dataclass(frozen=True)
class DedupeConfig:
    candidate_phash: int = 8
    fine_max: int = 12
    dhash_max: int = 10
    aspect_tolerance: float = 0.02
    same_moment: timedelta = field(default=timedelta(seconds=1))
    """Copies keep identical EXIF times. EXIF has whole seconds, so burst frames taken within
    the same second can still pass this check; the hashes have to tell them apart."""


@dataclass(frozen=True)
class PhotoRecord:
    id: int
    source: Source
    key: str
    sha256: str | None
    phash: str | None
    dhash: str | None
    fine: str | None
    width: int | None
    height: int | None
    taken: datetime | None
    """The capture time embedded in the file (EXIF), for the same-moment check."""
    content_id: str | None
    has_date: bool
    has_gps: bool
    has_caption: bool


def near_pairs(
    hashes: Sequence[int], max_distance: int, *, block: int = 4096
) -> list[tuple[int, int, int]]:
    """All index pairs (i < j) whose 64-bit hashes differ by at most ``max_distance`` bits."""
    values = np.asarray(hashes, dtype=np.uint64)
    pairs: list[tuple[int, int, int]] = []
    for start in range(0, len(values), block):
        rows = values[start : start + block]
        for other in range(start, len(values), block):
            columns = values[other : other + block]
            distances = np.bitwise_count(rows[:, None] ^ columns[None, :])
            for i, j in zip(*np.nonzero(distances <= max_distance), strict=True):
                a, b = start + int(i), other + int(j)
                if a < b:
                    pairs.append((a, b, int(distances[i, j])))
    return pairs


def _aspect(record: PhotoRecord) -> float | None:
    if not record.width or not record.height:
        return None
    return max(record.width, record.height) / min(record.width, record.height)


def _verified(a: PhotoRecord, b: PhotoRecord, config: DedupeConfig) -> bool:
    if a.fine and b.fine and hamming(a.fine, b.fine) > config.fine_max:
        return False
    if a.dhash and b.dhash and hamming(a.dhash, b.dhash) > config.dhash_max:
        return False
    ratio_a, ratio_b = _aspect(a), _aspect(b)
    if (
        ratio_a
        and ratio_b
        and abs(ratio_a - ratio_b) / max(ratio_a, ratio_b) > config.aspect_tolerance
    ):
        return False
    return not (a.taken and b.taken and not same_moment(a.taken, b.taken, config.same_moment))


def _metadata(record: PhotoRecord) -> int:
    return int(record.has_date) + int(record.has_gps) + int(record.has_caption)


def keeper_order(records: Iterable[PhotoRecord]) -> list[PhotoRecord]:
    """Best keeper first: resolution, then metadata, then iCloud, then the key."""
    return sorted(
        records,
        key=lambda r: (
            -((r.width or 0) * (r.height or 0)),
            -_metadata(r),
            r.source != "icloud",
            r.key,
        ),
    )


def find_duplicates(records: Sequence[PhotoRecord], config: DedupeConfig) -> list[DuplicateGroup]:
    groups = UnionFind(r.id for r in records)
    for first, second in same_value_pairs(records, "sha256"):
        groups.join(first, second, "exact")
    for first, second in same_value_pairs(records, "content_id"):
        groups.join(first, second, "content-id")
    hashed = [r for r in records if r.phash]
    for i, j, _ in near_pairs([int(r.phash or "0", 16) for r in hashed], config.candidate_phash):
        a, b = hashed[i], hashed[j]
        if groups.find(a.id) != groups.find(b.id) and _verified(a, b, config):
            groups.join(a.id, b.id, "near")
    return groups.groups(records, keeper_order)


# --- the stage -----------------------------------------------------------------------------

_LEFT_OUT = frozenset({"edited", "google-creation"})


def _record(catalogue: Catalogue, item: CatalogueItem) -> PhotoRecord:
    found = catalogue.date_candidates(cast(int, item.id))
    planned = json.loads(item.metadata_plan).get("taken")
    return PhotoRecord(
        id=cast(int, item.id),
        source=item.source,
        key=item.source_key,
        sha256=item.sha256,
        phash=item.phash,
        dhash=item.dhash,
        fine=item.phash_fine,
        width=item.width,
        height=item.height,
        taken=found.get("exif"),
        content_id=item.content_id,
        has_date=bool(found.get("exif") or found.get("icloud") or planned),
        has_gps=item.latitude is not None,
        has_caption=bool(item.description),
    )


class DedupeStage:
    """Propose photo duplicate groups (``groups.kind = 'duplicate'``) with their keepers."""

    name = "dedupe-photos"

    def __init__(self, config: DedupeConfig | None = None) -> None:
        self.config = config or DedupeConfig()

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        records = [
            _record(catalogue, item)
            for item in catalogue.items()
            if item.kind == "photo"
            and (item.sha256 or item.phash)
            and not (item.source == "google" and _LEFT_OUT & item.flags)
        ]
        by_id = {r.id: r for r in records}
        motions: dict[int, tuple[Source, str, str]] = {}
        for group in catalogue.groups("live-pair"):
            pair = catalogue.group_members(group.id)
            if len(pair) == 2:
                motion = catalogue.item(pair[1][0])
                motions[pair[0][0]] = (motion.source, motion.source_key, "motion")
        found = find_duplicates(records, self.config)
        yield from proposal_actions(catalogue, found, by_id, "dup-", motions)

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        apply_proposal(action, catalogue)
