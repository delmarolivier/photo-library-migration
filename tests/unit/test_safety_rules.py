"""GuardRules decides which paths count as real user media (CLAUDE.md rule 4)."""

import os
import pwd
import string
from pathlib import Path, PurePosixPath

import pytest
from hypothesis import given
from hypothesis import strategies as st

from photomigrate.safety import GuardRules

FAKE = Path("/photomigrate-fake")
HOME = FAKE / "home"
PICTURES = HOME / "Pictures"
SCRATCH = FAKE / "tmp"

RULES = GuardRules(forbidden_roots=(PICTURES,), allowed_roots=(SCRATCH,))


@pytest.mark.parametrize(
    "path",
    [
        PICTURES,
        PICTURES / "Photos Library.photoslibrary" / "database" / "Photos.sqlite",
        HOME / "Pictures" / "Exports" / "IMG_0001.HEIC",
        Path("/Volumes/Backup/Old Library.photoslibrary/originals/A/x.heic"),
        Path("/Volumes/Backup/Old Library.PhotosLibrary"),
        HOME / "Downloads" / "Takeout" / "Google Photos" / "IMG_0001.jpg",
        HOME / "Downloads" / "takeout" / "archive_browser.html",
        HOME / "Downloads" / "takeout-20261020T101010Z-001.zip",
        HOME / "Downloads" / "takeout-20261020T101010Z-001.tgz",
        SCRATCH / ".." / "home" / "Pictures" / "x.jpg",
    ],
)
def test_real_media_locations_are_forbidden(path: Path) -> None:
    assert RULES.is_forbidden(path)


@pytest.mark.parametrize(
    "path",
    [
        HOME / "Picturesque" / "x.jpg",
        HOME / "Documents" / "notes.txt",
        SCRATCH / "Disposable.photoslibrary" / "database" / "Photos.sqlite",
        SCRATCH / "Takeout" / "Google Photos" / "IMG_0001.jpg",
        SCRATCH / "takeout-20261020T101010Z-001.zip",
        FAKE / "src" / "photomigrate" / "ingest" / "takeout.py",
        FAKE / "takeout-notes.txt",
    ],
)
def test_ordinary_and_scratch_locations_are_allowed(path: Path) -> None:
    assert not RULES.is_forbidden(path)


def test_accepts_str_bytes_and_pure_paths() -> None:
    target = PICTURES / "x.jpg"

    assert RULES.is_forbidden(str(target))
    assert RULES.is_forbidden(os.fsencode(target))
    assert RULES.is_forbidden(PurePosixPath(target))


def test_forbidden_root_wins_over_allowed_root() -> None:
    real_export = SCRATCH / "real-export"
    rules = GuardRules(forbidden_roots=(real_export,), allowed_roots=(SCRATCH,))

    assert rules.is_forbidden(real_export / "IMG_0001.jpg")
    assert not rules.is_forbidden(SCRATCH / "fixture" / "IMG_0001.jpg")


def test_relative_paths_resolve_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    rules = GuardRules(forbidden_roots=(work,), allowed_roots=())

    assert rules.is_forbidden("IMG_0001.jpg")


def test_symlink_into_forbidden_root_is_forbidden(tmp_path: Path) -> None:
    pictures = tmp_path / "home" / "Pictures"
    pictures.mkdir(parents=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "pics").symlink_to(pictures, target_is_directory=True)
    rules = GuardRules(forbidden_roots=(pictures,), allowed_roots=(scratch,))

    assert rules.is_forbidden(scratch / "pics" / "IMG_0001.jpg")


def test_symlinked_allowed_root_still_allows_its_real_location(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    alias = tmp_path / "scratch-alias"
    alias.symlink_to(scratch, target_is_directory=True)
    rules = GuardRules(forbidden_roots=(), allowed_roots=(alias,))

    assert not rules.is_forbidden(scratch / "Takeout" / "IMG_0001.jpg")


def test_for_current_user_forbids_both_real_and_env_home_pictures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    extra = tmp_path / "real-takeout"

    rules = GuardRules.for_current_user(allowed_roots=[SCRATCH], extra_forbidden=[extra])

    assert Path(pwd.getpwuid(os.getuid()).pw_dir) / "Pictures" in rules.forbidden_roots
    assert tmp_path / "fake-home" / "Pictures" in rules.forbidden_roots
    assert extra in rules.forbidden_roots
    assert rules.allowed_roots == (SCRATCH,)


_plain_names = st.text(
    alphabet=string.ascii_letters + string.digits + " _-", min_size=1, max_size=12
)
_plain_segments = st.lists(
    _plain_names.filter(lambda s: s.strip(" ") == s and s.casefold() != "takeout"),
    min_size=1,
    max_size=5,
)


@given(segments=_plain_segments)
def test_anything_under_pictures_is_forbidden(segments: list[str]) -> None:
    assert RULES.is_forbidden(PICTURES.joinpath(*segments))


@given(segments=_plain_segments)
def test_plain_paths_under_scratch_are_allowed(segments: list[str]) -> None:
    assert not RULES.is_forbidden(SCRATCH.joinpath(*segments))


def test_relative_media_store_names_from_an_ordinary_cwd_are_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # shutil.rmtree and os.fwalk pass names relative to a directory fd, not the cwd. The fd
    # itself was checked when it was opened, so a bare name must not be judged against the cwd.
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    rules = GuardRules(forbidden_roots=(), allowed_roots=())

    assert not rules.is_forbidden("Disposable.photoslibrary")
    assert not rules.is_forbidden("Takeout")


def test_relative_paths_inside_a_real_takeout_cwd_are_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeout = tmp_path / "Downloads" / "Takeout"
    takeout.mkdir(parents=True)
    monkeypatch.chdir(takeout)
    rules = GuardRules(forbidden_roots=(), allowed_roots=())

    assert rules.is_forbidden("IMG_0001.jpg")


def test_a_bare_name_matching_a_forbidden_root_is_judged_by_the_cwd_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pytest's tmp cleanup passes bare names like "data-in" relative to a directory fd. Joining
    # them to the cwd would wrongly hit <repo>/data-in. Code in tests uses absolute paths.
    repo = tmp_path / "repo"
    (repo / "data-in").mkdir(parents=True)
    monkeypatch.chdir(repo)
    rules = GuardRules(forbidden_roots=(repo / "data-in",), allowed_roots=())

    assert not rules.is_forbidden("data-in")
    assert rules.is_forbidden(repo / "data-in" / "Timeline.json")
