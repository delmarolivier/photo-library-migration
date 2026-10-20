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

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, cast

import numpy as np

from photomigrate.catalogue import Source
from photomigrate.dates import same_moment
from photomigrate.hashing import hamming

Method = Literal["exact", "content-id", "near", "mixed"]


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


@dataclass(frozen=True)
class DuplicateGroup:
    keeper: int
    members: tuple[int, ...]
    """Record ids, keeper first, then in keeper order."""
    method: Method


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


class _Groups:
    def __init__(self, ids: Iterable[int]) -> None:
        self.parent = {i: i for i in ids}
        self.kinds: dict[int, set[str]] = {}

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def join(self, a: int, b: int, kind: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        kinds = self.kinds.pop(root_a, set()) | self.kinds.pop(root_b, set()) | {kind}
        self.parent[root_b] = root_a
        self.kinds[root_a] = kinds


def _same(records: Iterable[PhotoRecord], attribute: str) -> Iterable[tuple[int, int]]:
    seen: dict[str, int] = {}
    for record in records:
        value = getattr(record, attribute)
        if value:
            if value in seen:
                yield seen[value], record.id
            else:
                seen[value] = record.id


def find_duplicates(records: Sequence[PhotoRecord], config: DedupeConfig) -> list[DuplicateGroup]:
    groups = _Groups(r.id for r in records)
    for first, second in _same(records, "sha256"):
        groups.join(first, second, "exact")
    for first, second in _same(records, "content_id"):
        groups.join(first, second, "content-id")
    hashed = [r for r in records if r.phash]
    for i, j, _ in near_pairs([int(r.phash or "0", 16) for r in hashed], config.candidate_phash):
        a, b = hashed[i], hashed[j]
        if groups.find(a.id) != groups.find(b.id) and _verified(a, b, config):
            groups.join(a.id, b.id, "near")

    by_root: dict[int, list[PhotoRecord]] = {}
    for record in records:
        by_root.setdefault(groups.find(record.id), []).append(record)
    result = []
    for root, members in by_root.items():
        if len(members) < 2:
            continue
        ordered = keeper_order(members)
        kinds = groups.kinds[root]
        method = cast(Method, "mixed" if len(kinds) > 1 else next(iter(kinds)))
        result.append(DuplicateGroup(ordered[0].id, tuple(r.id for r in ordered), method))
    return sorted(result, key=lambda g: g.members)
