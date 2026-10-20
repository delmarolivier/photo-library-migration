"""Which library Photos has open comes from osxphotos (the real preferences are not read)."""

import pytest

from photomigrate.importer import library

pytestmark = pytest.mark.macos


def test_the_open_library_is_photos_last_library(monkeypatch: pytest.MonkeyPatch) -> None:
    import osxphotos.utils

    monkeypatch.setattr(osxphotos.utils, "get_last_library_path", lambda: "/x/Test.photoslibrary")

    assert library._open_library() == "/x/Test.photoslibrary"


def test_photoscript_is_the_real_module() -> None:
    assert library._photoscript().__name__ == "photoscript"
