"""Quality measurements: raw numbers per photo and video (CLA-83).

They are measured once, at ingest or index, while the file is at hand. The ``quality``
analysis stage then compares them with the thresholds in the config file and records flags
with scores. Nothing here decides anything.

Photos are measured on the upright image, downscaled so its long side is at most 1024 px,
which makes sharpness comparable across camera resolutions:

* ``sharpness``: the variance of the Laplacian (low means blurry);
* ``brightness``: the mean luminance (0 to 255);
* ``dark_fraction`` and ``bright_fraction``: the share of pixels below 16 and above 240;
* ``contrast``: the luminance standard deviation (near 0 means a flat frame: a pocket shot);
* ``megapixels`` and ``short_side``, from the full-size image.

Videos use the frames already sampled for their signature: mean ``brightness``, and
``motion``, the mean absolute difference between consecutive frames.
"""

from __future__ import annotations

import itertools
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError
from scipy.ndimage import laplace

pillow_heif.register_heif_opener()

_MEASURE_SIZE = (1024, 1024)
_ORIENTATION = 0x0112
_QUARTER_TURNS = frozenset({5, 6, 7, 8})  # EXIF orientations stored sideways


@dataclass(frozen=True)
class PhotoMetrics:
    sharpness: float
    brightness: float
    dark_fraction: float
    bright_fraction: float
    contrast: float
    megapixels: float
    short_side: int

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, float | int]) -> PhotoMetrics:
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class VideoMetrics:
    brightness: float
    motion: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, float]) -> VideoMetrics:
        return cls(**data)


def photo_metrics(path: Path) -> PhotoMetrics | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                if image.getexif().get(_ORIENTATION) in _QUARTER_TURNS:
                    width, height = height, width
                image.draft("L", _MEASURE_SIZE)
                grey = ImageOps.exif_transpose(image).convert("L")
                grey.thumbnail(_MEASURE_SIZE)
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return None
    pixels = np.asarray(grey, dtype=np.float64)
    return PhotoMetrics(
        sharpness=round(float(laplace(pixels).var()), 3),
        brightness=round(float(pixels.mean()), 3),
        dark_fraction=round(float((pixels < 16).mean()), 4),
        bright_fraction=round(float((pixels > 240).mean()), 4),
        contrast=round(float(pixels.std()), 3),
        megapixels=round(width * height / 1e6, 4),
        short_side=min(width, height),
    )


def video_metrics(frames: Sequence[Image.Image]) -> VideoMetrics:
    stack = [np.asarray(frame, dtype=np.float64) for frame in frames]
    motion = (
        float(np.mean([np.abs(b - a).mean() for a, b in itertools.pairwise(stack)]))
        if len(stack) > 1
        else 0.0
    )
    return VideoMetrics(
        brightness=round(float(np.mean([f.mean() for f in stack])), 3), motion=round(motion, 3)
    )
