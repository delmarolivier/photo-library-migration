"""Apple Vision and Core Image on real photos with people (osxphotos' test library)."""

import io
from pathlib import Path

import pytest

from photomigrate.faces import VisionFaces, face_reader
from photomigrate.hashing import open_picture
from tests.macos.conftest import LIBRARIES

pytestmark = pytest.mark.macos

WEDDING = "D/DE2FCB28-0078-4A78-BDF6-2B53F8ACF9B7.jpeg"
PUMPKINS_BLINK = "F/FC37DBA6-CF22-4E3E-AB70-755587454608.jpeg"
PUMPKINS_GROUP = "1/17BC6605-3A6D-472E-B257-1E5B37684F16.jpeg"
TULIPS = "7/70B328ED-8A80-498E-9034-459260380134.jpeg"


def read(osxphotos_libraries: Path, name: str) -> list:  # type: ignore[type-arg]
    picture = open_picture(osxphotos_libraries / LIBRARIES[0] / "originals" / name)
    assert picture is not None
    return VisionFaces().read(picture.image)


def test_vision_is_the_face_reader_on_macos() -> None:
    assert isinstance(face_reader(), VisionFaces)


def test_a_portrait_has_one_good_face_with_open_eyes(osxphotos_libraries: Path) -> None:
    (face,) = read(osxphotos_libraries, WEDDING)

    assert face.quality > 0.5
    assert face.eyes_open is True
    x, y, width, height = face.box
    assert 0 <= x < x + width <= 1
    assert 0 <= y < y + height <= 1


def test_closed_eyes_are_seen(osxphotos_libraries: Path) -> None:
    found = read(osxphotos_libraries, PUMPKINS_BLINK)

    assert [face.eyes_open for face in found] == [False]


def test_every_face_in_a_group_is_found(osxphotos_libraries: Path) -> None:
    found = read(osxphotos_libraries, PUMPKINS_GROUP)

    assert len(found) == 3
    assert all(0 < face.quality <= 1 for face in found)


def test_a_photo_without_people_has_no_faces(osxphotos_libraries: Path) -> None:
    assert read(osxphotos_libraries, TULIPS) == []


def test_a_picture_vision_cannot_read_has_no_face_measurement(
    osxphotos_libraries: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import Vision

    class Refusing:
        @classmethod
        def alloc(cls) -> "Refusing":
            return cls()

        def initWithData_options_(self, data: object, options: object) -> "Refusing":  # noqa: N802
            return self

        def performRequests_error_(self, requests: object, error: object) -> tuple[bool, str]:  # noqa: N802
            return False, "unreadable"

    monkeypatch.setattr(Vision, "VNImageRequestHandler", Refusing)

    assert read(osxphotos_libraries, WEDDING) is None


def _footprint_mb() -> float:
    """This process's physical footprint, as macOS counts it when it runs out of memory."""
    import os
    import subprocess

    out = subprocess.run(
        ["footprint", "-p", str(os.getpid())], capture_output=True, text=True, check=True
    ).stdout
    line = next(line for line in out.splitlines() if "Footprint:" in line)
    amount, unit = line.split("Footprint:")[1].split()[:2]
    return float(amount) * {"KB": 1 / 1024, "MB": 1.0, "GB": 1024.0}[unit]


def test_reading_faces_and_text_does_not_keep_the_memory_of_each_picture() -> None:
    # On macOS 27 a BytesIO that grows never hands its memory back, and Foundation objects
    # wait for an autorelease pool that a Python loop never drains: 165 MB per 12 MP photo
    # was kept, and ingest was killed after about 6,000 photos.
    from PIL import Image

    from photomigrate.faces import vision_data
    from photomigrate.text_regions import VisionText

    picture = Image.new("RGB", (4032, 3024), "gray")
    assert Image.open(io.BytesIO(bytes(vision_data(picture)))).size == (4032, 3024)
    faces, text = VisionFaces(), VisionText()
    faces.read(picture), text.read(picture)  # Vision's own caches, once
    before = _footprint_mb()
    for _ in range(10):
        faces.read(picture)
        text.read(picture)
    assert _footprint_mb() - before < 300
