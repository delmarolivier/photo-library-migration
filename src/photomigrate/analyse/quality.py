"""Quality flags: each item's measurements against the configured thresholds (CLA-83).

Ingest and index measure every item once (``photomigrate.quality``). This stage only
compares, so changing a threshold and running ``analyse`` again is quick. Each flag comes
with a score from 0 (just past the threshold) to 1 (as bad as it gets). Flags are hints for
the review gallery and the best-shot pick; nothing is decided or hidden here.

Photos: ``blurry``, ``low-resolution``, ``under-exposed``, ``over-exposed``, ``accidental``
(a flat frame: pocket shot, black frame). Videos: ``short-clip`` (never a Live Photo's motion
half, paired or not), ``black-video``, ``static-video``, ``tiny-video``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import cast

from photomigrate.catalogue import Catalogue, CatalogueItem, Source
from photomigrate.config import PhotoThresholds, QualityConfig, VideoThresholds
from photomigrate.quality import PhotoMetrics, VideoMetrics
from photomigrate.runner import Action, JSONValue

_MAX_LEVEL = 255.0


def _below(value: float, threshold: float) -> float | None:
    """The score of a value under its threshold: 0 at the threshold, 1 at zero."""
    if value >= threshold:
        return None
    return max(round(1 - value / threshold, 3), 0.001)


def _above(value: float, threshold: float, ceiling: float) -> float | None:
    """The score of a value over its threshold: 0 at the threshold, 1 at the ceiling."""
    if value <= threshold:
        return None
    return max(round(min((value - threshold) / (ceiling - threshold), 1.0), 3), 0.001)


def _worst(*scores: float | None) -> float | None:
    found = [s for s in scores if s is not None]
    return max(found) if found else None


def _flags(scores: dict[str, float | None]) -> dict[str, float]:
    return {flag: score for flag, score in scores.items() if score is not None}


def photo_flags(metrics: PhotoMetrics | None, t: PhotoThresholds) -> dict[str, float]:
    if metrics is None:
        return {}
    return _flags(
        {
            "blurry": _below(metrics.sharpness, t.blurry_below_sharpness),
            "low-resolution": _below(metrics.megapixels, t.low_resolution_below_megapixels),
            "under-exposed": _worst(
                _below(metrics.brightness, t.under_exposed_below_brightness),
                _above(metrics.dark_fraction, t.dark_above_fraction, 1.0),
            ),
            "over-exposed": _worst(
                _above(metrics.brightness, t.over_exposed_above_brightness, _MAX_LEVEL),
                _above(metrics.bright_fraction, t.bright_above_fraction, 1.0),
            ),
            "accidental": _below(metrics.contrast, t.accidental_below_contrast),
        }
    )


def video_flags(
    metrics: VideoMetrics | None,
    *,
    duration: float | None,
    short_side: int | None,
    motion_half: bool,
    t: VideoThresholds,
) -> dict[str, float]:
    """``motion_half``: the clip is a Live Photo's motion, so its shortness is expected."""
    scores: dict[str, float | None] = {}
    if duration is not None and not motion_half:
        scores["short-clip"] = _below(duration, t.short_below_seconds)
    if short_side is not None:
        scores["tiny-video"] = _below(short_side, t.tiny_below_short_side)
    if metrics is not None:
        scores["black-video"] = _below(metrics.brightness, t.black_below_brightness)
        scores["static-video"] = _below(metrics.motion, t.static_below_motion)
    return _flags(scores)


class QualityStage:
    name = "quality"

    def __init__(self, config: QualityConfig) -> None:
        self.config = config

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        motion = {
            item_id
            for group in catalogue.groups("live-pair")
            for item_id, role, _ in catalogue.group_members(group.id)
            if role == "motion"
        }
        stored = catalogue.quality_flags()
        for item in catalogue.items():
            item_id = cast(int, item.id)  # rows read back from the catalogue have ids
            flags = self.flags(item, motion_half=item_id in motion)
            if flags != stored.get(item_id, {}):
                details: dict[str, JSONValue] = {"source": item.source, "flags": dict(flags)}
                yield Action("set-quality-flags", item.source_key, details)

    def flags(self, item: CatalogueItem, *, motion_half: bool) -> dict[str, float]:
        measured = json.loads(item.quality)
        if item.kind == "photo":
            metrics = PhotoMetrics.from_dict(measured) if measured else None
            return photo_flags(metrics, self.config.photo)
        sides = [side for side in (item.width, item.height) if side]
        return video_flags(
            VideoMetrics.from_dict(measured) if measured else None,
            duration=item.duration,
            short_side=min(sides) if len(sides) == 2 else None,
            motion_half=motion_half or "unpaired-motion" in item.flags,
            t=self.config.video,
        )

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        source = cast(Source, action.details["source"])
        item_id = cast(int, catalogue.item_for_key(source, action.target))
        flags = cast(dict[str, float], action.details["flags"])
        catalogue.set_quality_flags(item_id, flags)
