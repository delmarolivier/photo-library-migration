"""`photomigrate index` refuses things that are not Photos libraries, and works only on macOS."""

import sys
from pathlib import Path

import pytest

from photomigrate.index.icloud import IndexStage, LibraryError


def fake_library(tmp_path: Path) -> Path:
    library = tmp_path / "Fake.photoslibrary"
    (library / "database").mkdir(parents=True)
    (library / "database" / "Photos.sqlite").write_bytes(b"")
    (library / "originals").mkdir()
    return library


def test_without_osxphotos_indexing_explains_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "osxphotos", None)

    with IndexStage(fake_library(tmp_path)) as stage, pytest.raises(LibraryError, match="macOS"):
        list(stage.plan(None))  # type: ignore[arg-type]


def test_a_folder_without_a_photos_database_is_refused(tmp_path: Path) -> None:
    with IndexStage(tmp_path) as stage, pytest.raises(LibraryError, match="not a Photos library"):
        list(stage.plan(None))  # type: ignore[arg-type]


def test_the_shadow_library_is_a_database_copy_plus_symlinks(tmp_path: Path) -> None:
    library = fake_library(tmp_path)

    with IndexStage(library) as stage:
        shadow = stage._shadow_library()
        assert (shadow / "database" / "Photos.sqlite").is_file()
        assert not (shadow / "database").is_symlink()
        assert (shadow / "originals").is_symlink()
        assert (shadow / "originals").resolve() == (library / "originals").resolve()
        assert stage.read_only.violations == []

    assert not shadow.exists()
    assert (library / "originals").is_dir(), "cleaning up the shadow must not touch the library"


def test_the_shadow_falls_back_to_a_plain_copy_off_apfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from photomigrate.index import icloud

    def no_clone(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(icloud.subprocess, "run", no_clone)
    library = fake_library(tmp_path)
    monkeypatch.setattr(icloud.sys, "platform", "darwin")

    with IndexStage(library) as stage:
        shadow = stage._shadow_library()
        assert (shadow / "database" / "Photos.sqlite").is_file()


def test_missing_originals_are_listed_up_to_a_limit() -> None:
    import io

    from rich.console import Console

    from photomigrate.cli import warn_missing_originals
    from photomigrate.runner import Action

    out = io.StringIO()
    actions = [
        Action("index-missing", f"UUID-{n}", {"filename": f"IMG_{n}.HEIC"}) for n in range(3)
    ]

    warn_missing_originals(actions, Console(file=out, width=200), limit=2)
    warn_missing_originals([], Console(file=out, width=200))

    text = out.getvalue()
    assert "3 originals are not on this Mac" in text
    assert "IMG_1.HEIC (UUID-1)" in text
    assert "IMG_2.HEIC" not in text
    assert "… and 1 more" in text


def test_an_item_without_a_date_or_original_is_still_catalogued(tmp_path: Path) -> None:
    from photomigrate.catalogue import Catalogue
    from photomigrate.runner import Action

    details = {
        "original": None, "filename": "IMG_1.HEIC", "kind": "photo", "date": None,
        "timezone": None, "latitude": None, "longitude": None, "width": None, "height": None,
        "favorite": False, "caption": None, "albums": [], "keywords": [], "scores": {},
        "live_motion": None, "flags": [],
    }  # fmt: skip

    with IndexStage(fake_library(tmp_path)) as stage, Catalogue.snapshot(tmp_path / "c") as cat:
        stage.apply(Action("index-missing", "UUID-1", details), cat)  # type: ignore[arg-type]
        (item,) = cat.items("icloud")
        assert "original-missing" in item.flags
        assert cat.date_candidates(item.id or 0) == {}


def test_the_shadow_is_a_plain_copy_on_other_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from photomigrate.index import icloud

    monkeypatch.setattr(icloud.sys, "platform", "linux")

    with IndexStage(fake_library(tmp_path)) as stage:
        assert (stage._shadow_library() / "database" / "Photos.sqlite").is_file()
