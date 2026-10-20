"""Content hashes: SHA-256 for byte-identical copies, perceptual hashes for near-duplicates.

Perceptual hashes are computed on the upright image (EXIF orientation applied), so a rotated
copy still matches. Each photo is decoded once (``open_picture``) for its hashes and its
quality metrics; big JPEGs are decoded at reduced size, which is much faster.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import imagehash
import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError

pillow_heif.register_heif_opener()

_PICTURE_SIZE = (1024, 1024)
_HASH_SIZE = (512, 512)
_ORIENTATION = 0x0112
_QUARTER_TURNS = frozenset({5, 6, 7, 8})  # EXIF orientations stored sideways


@dataclass(frozen=True)
class ImageHashes:
    """``phash`` and ``dhash``: 64 bits each, fast to compare (finding candidates). ``fine``: a
    256-bit pHash that tells copies (distance ≤ 8 in tests) from crops (≥ 18)."""

    phash: str
    dhash: str
    fine: str

    def __str__(self) -> str:
        return f"{self.phash}:{self.dhash}:{self.fine}"

    @classmethod
    def parse(cls, text: str) -> ImageHashes:
        phash, dhash, fine = text.split(":")
        return cls(phash, dhash, fine)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Picture:
    """A photo decoded once, for hashing and measuring: upright RGB with the long side at most
    1024 px, and the full upright ``size``."""

    image: Image.Image
    size: tuple[int, int]


def open_picture(path: Path) -> Picture | None:
    """Decode a photo, or ``None`` if it cannot be decoded as one."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                if image.getexif().get(_ORIENTATION) in _QUARTER_TURNS:
                    width, height = height, width
                image.draft("RGB", _PICTURE_SIZE)  # JPEG only: decode at a fraction of full size
                upright = ImageOps.exif_transpose(image)
                upright.thumbnail(_PICTURE_SIZE)
                rgb = upright.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return None
    return Picture(rgb, (width, height))


def picture_hashes(picture: Picture) -> ImageHashes:
    small = picture.image.copy()
    small.thumbnail(_HASH_SIZE)
    return ImageHashes(
        str(imagehash.phash(small)),
        str(imagehash.dhash(small)),
        str(imagehash.phash(small, hash_size=16)),
    )


def image_hashes(path: Path) -> ImageHashes | None:
    """Perceptual hashes of a picture, or ``None`` if it cannot be decoded as one."""
    picture = open_picture(path)
    return None if picture is None else picture_hashes(picture)


def hamming(a: str, b: str) -> int:
    """How many bits differ between two hex hashes of the same length."""
    if len(a) != len(b):
        raise ValueError("hashes of different length cannot be compared")
    return (int(a, 16) ^ int(b, 16)).bit_count()


_FRAME = 64


def sample_video(path: Path, duration: float | None, frames: int = 8) -> list[Image.Image] | None:
    """``frames`` frames sampled evenly across a video, as 64x64 greyscale images.

    One ffmpeg pass. ``None`` if the duration is unknown or ffmpeg cannot decode the file.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or not duration or duration <= 0:
        return None
    rate = frames / duration
    args = [
        ffmpeg, "-nostdin", "-loglevel", "error", "-i", str(path),
        "-vf", f"fps={rate:.6f},scale={_FRAME}:{_FRAME}:flags=area,format=gray",
        "-frames:v", str(frames), "-f", "rawvideo", "-",
    ]  # fmt: skip
    # Fixed argv: our own values plus the file path; no shell.
    done = subprocess.run(args, capture_output=True, check=False)  # noqa: S603
    size = _FRAME * _FRAME
    if done.returncode != 0 or len(done.stdout) < size:
        return None
    raw = done.stdout
    return [
        Image.frombytes("L", (_FRAME, _FRAME), raw[i : i + size])
        for i in range(0, len(raw) - size + 1, size)
    ]


def signature_of(frames: Sequence[Image.Image]) -> list[str]:
    """A video signature: the 64-bit pHash of each sampled frame, in order."""
    return [str(imagehash.phash(frame)) for frame in frames]


def video_signature(path: Path, duration: float | None, frames: int = 8) -> list[str] | None:
    """64-bit pHashes of ``frames`` frames sampled evenly across a video (see ``sample_video``)."""
    sampled = sample_video(path, duration, frames)
    return None if sampled is None else signature_of(sampled)


def signature_distance(a: Sequence[str], b: Sequence[str]) -> float:
    """Mean bit difference of aligned frames; 64 (all different) if nothing overlaps."""
    pairs = list(zip(a, b, strict=False))
    if not pairs:
        return 64.0
    return sum(hamming(x, y) for x, y in pairs) / len(pairs)


def bitrate(size: int | None, duration: float | None) -> int | None:
    """Average bits per second, from the file size and duration."""
    if not size or not duration or duration <= 0:
        return None
    return int(size * 8 / duration)
