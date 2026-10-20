"""Content hashes: SHA-256 for byte-identical copies, perceptual hashes for near-duplicates.

Perceptual hashes are computed on the upright image
(EXIF orientation applied), so a rotated copy still matches. Big JPEGs are decoded at reduced
size first, which is much faster and gives the same hash.
"""

from __future__ import annotations

import hashlib
import warnings
from dataclasses import dataclass
from pathlib import Path

import imagehash
import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError

pillow_heif.register_heif_opener()

_DECODE_SIZE = (512, 512)


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


def image_hashes(path: Path) -> ImageHashes | None:
    """Perceptual hashes of a picture, or ``None`` if it cannot be decoded as one."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.draft("RGB", _DECODE_SIZE)  # JPEG only: decode at a fraction of full size
                upright = ImageOps.exif_transpose(image)
                upright.thumbnail(_DECODE_SIZE)
                rgb = upright.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return None
    return ImageHashes(
        str(imagehash.phash(rgb)),
        str(imagehash.dhash(rgb)),
        str(imagehash.phash(rgb, hash_size=16)),
    )


def hamming(a: str, b: str) -> int:
    """How many bits differ between two hex hashes of the same length."""
    if len(a) != len(b):
        raise ValueError("hashes of different length cannot be compared")
    return (int(a, 16) ^ int(b, 16)).bit_count()
