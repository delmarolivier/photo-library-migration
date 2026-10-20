"""The config file (TOML, given with ``--config``). Every setting has a built-in default.

``photomigrate.example.toml`` lists every setting with its default (a test keeps the two in
step). A file only needs the settings it changes. Unknown sections or settings, wrong types
and negative values are errors, so a typo cannot silently fall back to a default.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_type_hints


class ConfigError(ValueError):
    """The config file is missing, not TOML, or has an unknown or invalid setting."""


@dataclass(frozen=True)
class PhotoThresholds:
    """When a photo gets each quality flag. Measurements are described in ``quality.py``."""

    blurry_below_sharpness: float = 30.0
    low_resolution_below_megapixels: float = 0.3
    under_exposed_below_brightness: float = 40.0
    over_exposed_above_brightness: float = 215.0
    dark_above_fraction: float = 0.5
    """Also under-exposed: more than this share of pixels near black."""
    bright_above_fraction: float = 0.5
    """Also over-exposed: more than this share of pixels near white."""
    accidental_below_contrast: float = 10.0
    """A flat frame: a pocket shot, a black frame, a lens cap."""


@dataclass(frozen=True)
class VideoThresholds:
    short_below_seconds: float = 1.5
    """Live Photo motion halves are never short clips."""
    black_below_brightness: float = 12.0
    static_below_motion: float = 2.0
    tiny_below_short_side: int = 240


@dataclass(frozen=True)
class QualityConfig:
    photo: PhotoThresholds = field(default_factory=PhotoThresholds)
    video: VideoThresholds = field(default_factory=VideoThresholds)


@dataclass(frozen=True)
class BestShotWeights:
    """How much each score counts in a shot's composite. Only their ratios matter, and a
    score some shot in the group lacks is left out for the whole group."""

    sharpness: float = 0.3
    exposure: float = 0.15
    faces: float = 0.2
    """Apple Vision's face capture quality (macOS)."""
    eyes_open: float = 0.2
    """The share of faces with both eyes open (macOS)."""
    aesthetic: float = 0.15
    """Apple's overall aesthetic score (iCloud items only)."""

    def __post_init__(self) -> None:
        if not any(getattr(self, name) > 0 for name in self.names()):
            raise ConfigError("at least one 'best_shot.weights' setting must be above 0")

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in dataclasses.fields(cls))


@dataclass(frozen=True)
class BestShotConfig:
    window_seconds: float = 120.0
    """Shots in one group are all taken within this long of the group's first."""
    min_similarity: float = 0.94
    """Cosine similarity of open_clip image embeddings, from -1 to 1. On the real sample, most
    pairs above 0.94 share their framing; below 0.92, most are other compositions."""
    weights: BestShotWeights = field(default_factory=BestShotWeights)

    def __post_init__(self) -> None:
        if self.min_similarity > 1:
            raise ConfigError("'best_shot.min_similarity' must be at most 1")


@dataclass(frozen=True)
class ClutterConfig:
    min_score: float = 0.5
    """An item joins the clutter queue when its best category scores at least this (0 to 1)."""

    def __post_init__(self) -> None:
        if self.min_score > 1:
            raise ConfigError("'clutter.min_score' must be at most 1")


@dataclass(frozen=True)
class LocationConfig:
    window_minutes: float = 30.0
    """A shot without GPS borrows from the same camera's shots within this long of it."""
    timeline_tolerance_minutes: float = 15.0
    """How far a Timeline path point may be from the moment, when no visit covers it."""
    any_device: bool = False
    """Also borrow from other cameras' shots (a shared event), not just the same device's."""


@dataclass(frozen=True)
class Config:
    quality: QualityConfig = field(default_factory=QualityConfig)
    best_shot: BestShotConfig = field(default_factory=BestShotConfig)
    clutter: ClutterConfig = field(default_factory=ClutterConfig)
    location: LocationConfig = field(default_factory=LocationConfig)


def load_config(path: Path | None) -> Config:
    """The built-in defaults, overridden by the file at ``path`` if one is given."""
    if path is None:
        return Config()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"no config file at {path}") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    try:
        return _build(Config, data, "")
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def _build[T](cls: type[T], data: dict[str, Any], prefix: str) -> T:
    hints = get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    values: dict[str, Any] = {}
    for name, value in data.items():
        where = prefix + name
        if name not in names:
            what = "section" if isinstance(value, dict) else "setting"
            raise ConfigError(f"unknown {what} {where!r}")
        hint = hints[name]
        if dataclasses.is_dataclass(hint):
            if not isinstance(value, dict):
                raise ConfigError(f"{where!r} must be a table, like [{where}]")
            values[name] = _build(hint, value, where + ".")  # type: ignore[arg-type]
        elif hint is bool:
            if not isinstance(value, bool):
                raise ConfigError(f"{where!r} must be true or false, not {value!r}")
            values[name] = value
        else:
            values[name] = _number(value, hint, where)
    return cls(**values)


def _number(value: Any, hint: type, where: str) -> float | int:
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where!r} must be a whole number, not {value!r}")
        number: float | int = value
    else:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{where!r} must be a number, not {value!r}")
        number = float(value)
    if number < 0:
        raise ConfigError(f"{where!r} must not be negative")
    return number
