"""Deterministic synthetic media for fixtures: seeded images with EXIF, and tiny real videos.

Same inputs give the same bytes (JPEG, PNG, GIF and ffmpeg's bit-exact H.264), so
generated Takeouts are reproducible. Videos need ``ffmpeg`` on PATH.
"""

from __future__ import annotations

import io
import random
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pillow_heif
from PIL import ExifTags, Image, ImageChops, ImageDraw

pillow_heif.register_heif_opener()

ImageFormat = Literal["JPEG", "HEIC", "PNG"]
Container = Literal["mp4", "mov"]


class MediaToolMissingError(RuntimeError):
    """An external tool needed to generate fixtures is not installed."""


@dataclass(frozen=True)
class Exif:
    """Camera EXIF to embed. ``taken`` is written as local wall time plus its UTC offset."""

    taken: datetime | None = None
    make: str | None = None
    model: str | None = None
    gps: tuple[float, float] | None = None


def utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        raise ValueError(f"fixture datetimes need a timezone: {when!r}")
    return when.astimezone(UTC)


def synthetic_image(
    seed: int, size: tuple[int, int] = (320, 240), *, jitter: int = 0
) -> Image.Image:
    """A seeded abstract picture. Different seeds give clearly different images.

    ``jitter`` moves every shape by up to that many pixels: the same scene a moment later.
    """
    rng = random.Random(seed)
    wobble = random.Random(seed * 7 + jitter)
    width, height = size
    image = Image.new("RGB", size, tuple(rng.randrange(256) for _ in range(3)))
    draw = ImageDraw.Draw(image)
    for _ in range(8):
        x0, x1 = sorted(rng.randrange(width) for _ in range(2))
        y0, y1 = sorted(rng.randrange(height) for _ in range(2))
        colour = tuple(rng.randrange(256) for _ in range(3))
        shape = draw.ellipse if rng.random() < 0.5 else draw.rectangle
        dx, dy = (wobble.randint(-jitter, jitter) for _ in range(2)) if jitter else (0, 0)
        shape((x0 + dx, y0 + dy, x1 + dx + 1, y1 + dy + 1), fill=colour)
    return image


def synthetic_photo(seed: int, size: tuple[int, int] = (800, 600)) -> Image.Image:
    """``synthetic_image`` with fine seeded grain, so it has the texture of a real, sharp photo
    (flat shapes alone measure as blurry)."""
    grain = random.Random(seed).randbytes(size[0] * size[1])
    noise = Image.frombytes("L", size, grain).convert("RGB")
    return Image.blend(synthetic_image(seed, size), noise, 0.12)


def video_frames(
    seed: int, seconds: float = 1.0, fps: int = 10, size: tuple[int, int] = (64, 48)
) -> list[Image.Image]:
    """Frames of a seeded image panning sideways, so consecutive frames differ."""
    base = synthetic_image(seed, size)
    return [ImageChops.offset(base, step * 2, 0) for step in range(round(seconds * fps))]


def scene_frames(
    seed: int, seconds: float = 2.0, fps: int = 10, size: tuple[int, int] = (320, 240)
) -> list[Image.Image]:
    """Frames of a lively video: a different seeded scene every half second."""
    return [synthetic_image(seed + step * 2 // fps, size) for step in range(round(seconds * fps))]


def _offset(offset: timedelta) -> str:
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _dms(value: float) -> tuple[float, float, float]:
    value = abs(value)
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = ((value - degrees) * 60 - minutes) * 60
    return float(degrees), float(minutes), seconds


def _exif_bytes(spec: Exif) -> bytes:
    exif = Image.Exif()
    if spec.make:
        exif[ExifTags.Base.Make] = spec.make
    if spec.model:
        exif[ExifTags.Base.Model] = spec.model
    if spec.taken:
        details = exif.get_ifd(ExifTags.IFD.Exif)
        details[ExifTags.Base.DateTimeOriginal] = f"{spec.taken:%Y:%m:%d %H:%M:%S}"
        offset = spec.taken.utcoffset()
        if offset is not None:
            details[ExifTags.Base.OffsetTimeOriginal] = _offset(offset)
    if spec.gps:
        latitude, longitude = spec.gps
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        gps[ExifTags.GPS.GPSLatitudeRef] = "N" if latitude >= 0 else "S"
        gps[ExifTags.GPS.GPSLatitude] = _dms(latitude)
        gps[ExifTags.GPS.GPSLongitudeRef] = "E" if longitude >= 0 else "W"
        gps[ExifTags.GPS.GPSLongitude] = _dms(longitude)
    return exif.tobytes()


def encode_image(
    image: Image.Image, fmt: ImageFormat, *, exif: Exif | None = None, quality: int = 85
) -> bytes:
    buffer = io.BytesIO()
    options: dict[str, object] = {"exif": _exif_bytes(exif)} if exif else {}
    if fmt != "PNG":
        options["quality"] = quality
    image.convert("RGB").save(buffer, "HEIF" if fmt == "HEIC" else fmt, **options)
    return buffer.getvalue()


def encode_gif(frames: Sequence[Image.Image], *, frame_ms: int = 100) -> bytes:
    buffer = io.BytesIO()
    first, *rest = frames
    first.save(buffer, "GIF", save_all=True, append_images=rest, duration=frame_ms, loop=0)
    return buffer.getvalue()


def encode_video(
    frames: Sequence[Image.Image],
    container: Container,
    *,
    fps: int = 10,
    created: datetime | None = None,
    content_id: str | None = None,
) -> bytes:
    """A real H.264 clip in an MP4 or QuickTime container.

    ``content_id`` sets Apple's Live Photo key (``com.apple.quicktime.content.identifier``).
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MediaToolMissingError(
            "ffmpeg is needed to generate fixture videos: "
            "brew install ffmpeg (or apt install ffmpeg)"
        )
    width, height = frames[0].size
    raw = b"".join(frame.convert("RGB").tobytes() for frame in frames)
    with tempfile.TemporaryDirectory(prefix="photomigrate-video-") as scratch:
        out = Path(scratch) / f"clip.{container}"
        args = [
            ffmpeg, "-nostdin", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps),
            "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1",
        ]  # fmt: skip
        if created:
            args += ["-metadata", f"creation_time={utc(created):%Y-%m-%dT%H:%M:%SZ}"]
        if content_id:
            args += [
                "-movflags", "use_metadata_tags",
                "-metadata", f"com.apple.quicktime.content.identifier={content_id}",
            ]  # fmt: skip
        # Fixed argv built from our own values; no shell.
        subprocess.run([*args, str(out)], input=raw, check=True, capture_output=True)  # noqa: S603
        return out.read_bytes()


def reencode_video(
    data: bytes,
    container: Container,
    *,
    scale: tuple[int, int] | None = None,
    crf: int | None = None,
) -> bytes:
    """``data`` re-encoded by ffmpeg, as a messaging app or an export would: same content,
    new bytes, optionally smaller and lower quality. Metadata is kept."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MediaToolMissingError("ffmpeg is needed to re-encode fixture videos")
    with tempfile.TemporaryDirectory(prefix="photomigrate-video-") as scratch:
        source, out = Path(scratch) / "in.bin", Path(scratch) / f"out.{container}"
        source.write_bytes(data)
        args = [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(source)]
        if scale:
            args += ["-vf", f"scale={scale[0]}:{scale[1]}"]
        args += [
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-crf", str(crf if crf is not None else 23),
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "0",
        ]  # fmt: skip
        # Fixed argv built from our own values; no shell.
        subprocess.run([*args, str(out)], check=True, capture_output=True)  # noqa: S603
        return out.read_bytes()
