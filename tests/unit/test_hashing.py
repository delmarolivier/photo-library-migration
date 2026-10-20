"""Content hashes: SHA-256 for exact copies, perceptual hashes for near-duplicates."""

import hashlib
import io
from pathlib import Path

import pytest
from photomigrate.hashing import ImageHashes, hamming, image_hashes, sha256_file
from PIL import Image

from photomigrate.testing.media import Exif, encode_image, synthetic_image


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_sha256_streams_the_file(tmp_path: Path) -> None:
    data = b"x" * 3_000_000

    assert sha256_file(write(tmp_path / "big.bin", data)) == hashlib.sha256(data).hexdigest()


def test_perceptual_hashes_are_hex_and_deterministic(tmp_path: Path) -> None:
    path = write(tmp_path / "a.jpg", encode_image(synthetic_image(1), "JPEG"))

    first, second = image_hashes(path), image_hashes(path)

    assert first == second
    assert first is not None
    assert len(first.phash) == 16
    assert len(first.dhash) == 16
    int(first.phash, 16)


@pytest.mark.parametrize("variant", ["resized", "recompressed", "heic"])
def test_near_duplicates_have_close_hashes(tmp_path: Path, variant: str) -> None:
    image = synthetic_image(7)
    original = image_hashes(write(tmp_path / "a.jpg", encode_image(image, "JPEG")))
    if variant == "resized":
        data = encode_image(image.resize((160, 120)), "JPEG")
    elif variant == "recompressed":
        data = encode_image(image, "JPEG", quality=25)
    else:
        data = encode_image(image, "HEIC")
    near = image_hashes(write(tmp_path / f"b.{'heic' if variant == 'heic' else 'jpg'}", data))

    assert original is not None
    assert near is not None
    assert hamming(original.phash, near.phash) <= 6
    assert hamming(original.dhash, near.dhash) <= 10


def test_different_pictures_have_distant_hashes(tmp_path: Path) -> None:
    a = image_hashes(write(tmp_path / "a.jpg", encode_image(synthetic_image(1), "JPEG")))
    b = image_hashes(write(tmp_path / "b.jpg", encode_image(synthetic_image(2), "JPEG")))

    assert a is not None
    assert b is not None
    assert hamming(a.phash, b.phash) > 12


def test_exif_orientation_is_applied_before_hashing(tmp_path: Path) -> None:
    image = synthetic_image(3)
    upright = image_hashes(write(tmp_path / "up.jpg", encode_image(image, "JPEG")))
    rotated = image.rotate(90, expand=True)
    exif = Image.Exif()
    exif[0x0112] = 6  # stored rotated; a viewer turns it back upright
    buffer = io.BytesIO()
    rotated.save(buffer, "JPEG", exif=exif.tobytes(), quality=85)
    tagged = image_hashes(write(tmp_path / "tagged.jpg", buffer.getvalue()))

    assert upright is not None
    assert tagged is not None
    assert hamming(upright.phash, tagged.phash) <= 6


def test_files_that_are_not_images_give_no_hashes(tmp_path: Path) -> None:
    assert image_hashes(write(tmp_path / "clip.mov", b"\x00\x00\x00\x18ftypqt  ")) is None
    assert image_hashes(write(tmp_path / "broken.jpg", b"\xff\xd8 not really")) is None


def test_hamming_counts_differing_bits() -> None:
    assert hamming("ff00", "ff00") == 0
    assert hamming("ff00", "fe01") == 2
    with pytest.raises(ValueError, match="length"):
        hamming("ff", "ffff")


def test_hashes_round_trip_through_a_string() -> None:
    hashes = ImageHashes(phash="0123456789abcdef", dhash="fedcba9876543210")

    assert ImageHashes.parse(str(hashes)) == hashes


def test_large_pictures_are_hashed_from_a_reduced_decode(tmp_path: Path) -> None:
    big = synthetic_image(4, size=(6000, 4000))
    path = write(tmp_path / "big.jpg", encode_image(big, "JPEG", exif=Exif(make="Apple")))
    small = write(tmp_path / "small.jpg", encode_image(big.resize((600, 400)), "JPEG"))

    a, b = image_hashes(path), image_hashes(small)

    assert a is not None
    assert b is not None
    assert hamming(a.phash, b.phash) <= 6
