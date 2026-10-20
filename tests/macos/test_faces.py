"""Apple Vision and Core Image on real photos with people (osxphotos' test library)."""

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
