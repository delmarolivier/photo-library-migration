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

The same decode gives the photo's open_clip embedding, its faces and its text regions
(``embedding``, ``faces``, ``text_regions``) when the caller passes an embedder and readers
(``measure_photo``), for best-shot and clutter.

Videos use the frames already sampled for their signature: mean ``brightness``, and
``motion``, the mean absolute difference between consecutive frames.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import laplace

from photomigrate.embedding import Embedder, to_bytes
from photomigrate.faces import FaceReader, faces_to_json
from photomigrate.hashing import (
    ImageHashes,
    Picture,
    open_picture,
    picture_hashes,
    sample_video,
    signature_of,
)
from photomigrate.text_regions import TextReader, text_to_json


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


def picture_metrics(picture: Picture) -> PhotoMetrics:
    pixels = np.asarray(picture.image.convert("L"), dtype=np.float64)
    width, height = picture.size
    return PhotoMetrics(
        sharpness=round(float(laplace(pixels).var()), 3),
        brightness=round(float(pixels.mean()), 3),
        dark_fraction=round(float((pixels < 16).mean()), 4),
        bright_fraction=round(float((pixels > 240).mean()), 4),
        contrast=round(float(pixels.std()), 3),
        megapixels=round(width * height / 1e6, 4),
        short_side=min(width, height),
    )


def photo_metrics(path: Path) -> PhotoMetrics | None:
    picture = open_picture(path)
    return None if picture is None else picture_metrics(picture)


@dataclass(frozen=True)
class PhotoMeasurement:
    """Everything measured from one decode of a photo; ``None`` where not measured."""

    hashes: ImageHashes | None = None
    metrics: PhotoMetrics | None = None
    embedding: bytes | None = None
    """As stored in the catalogue (``embedding.to_bytes``)."""
    faces: str | None = None
    """As stored in the catalogue (``faces.faces_to_json``)."""
    text: str | None = None
    """As stored in the catalogue (``text_regions.text_to_json``)."""


def measure_photo(
    path: Path,
    *,
    embedder: Embedder | None = None,
    faces: FaceReader | None = None,
    text: TextReader | None = None,
) -> PhotoMeasurement:
    """A photo's hashes and metrics, plus its embedding, faces and text when given an
    ``embedder`` and readers, from one decode. All ``None`` if it cannot be decoded."""
    picture = open_picture(path)
    if picture is None:
        return PhotoMeasurement()
    return PhotoMeasurement(
        hashes=picture_hashes(picture),
        metrics=picture_metrics(picture),
        embedding=to_bytes(embedder.embed(picture.image)) if embedder else None,
        faces=faces_to_json(faces.read(picture.image)) if faces else None,
        text=text_to_json(text.read(picture.image)) if text else None,
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


def measure_video(
    path: Path, duration: float | None
) -> tuple[list[str] | None, VideoMetrics | None]:
    """A video's signature and metrics, from one ffmpeg pass. ``None``s if it cannot be read."""
    frames = sample_video(path, duration)
    if frames is None:
        return None, None
    return signature_of(frames), video_metrics(frames)


def as_json(metrics: PhotoMetrics | VideoMetrics | None) -> str:
    """Metrics as stored in the catalogue's ``quality`` column (``{}``: not measured)."""
    return "{}" if metrics is None else json.dumps(metrics.to_dict(), sort_keys=True)
