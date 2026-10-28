"""Reading a Takeout: zip parts or an extracted tree, and safe, idempotent extraction."""

import hashlib
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from photomigrate.ingest.takeout import TakeoutError, open_takeout
from photomigrate.testing.takeout import PHOTOS_ROOT, TakeoutBuilder, TakeoutManifest

TAKEN = datetime(2019, 7, 1, 12, 0, tzinfo=UTC)


def small_takeout(dest: Path, parts: int = 2) -> TakeoutManifest:
    b = TakeoutBuilder(seed=3)
    b.photo("Photos from 2019", "IMG_1.JPG", taken=TAKEN)
    b.photo("Photos from 2019", "IMG_2.JPG", taken=TAKEN)
    b.album("Été 2019")
    b.other_product("Maps (your places)/Saved Places.json", b"{}")
    return b.write_zips(dest, parts=parts)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_zip_parts_are_listed_with_google_photos_entries_only(tmp_path: Path) -> None:
    manifest = small_takeout(tmp_path / "in")

    with open_takeout(tmp_path / "in") as takeout:
        assert [p.name for p in takeout.parts] == [z.name for z in manifest.zips]
        paths = [e.path for e in takeout.entries]
        assert paths == sorted(p for p in manifest.files if p.startswith(PHOTOS_ROOT))
        assert takeout.ignored == 2  # archive_browser.html and the Maps file
        entry = next(e for e in takeout.entries if e.path.endswith("IMG_1.JPG"))
        assert entry.part == manifest.zips[manifest.parts[entry.path] - 1].name
        assert entry.size > 0
        assert entry.crc32 is not None
        assert entry.modified == datetime(2026, 10, 20, 8, 42, 20)
        assert takeout.read(entry.path)[:2] == b"\xff\xd8"


def test_an_extracted_tree_is_read_in_place(tmp_path: Path) -> None:
    manifest = TakeoutBuilder(seed=3)
    manifest.photo("Photos from 2019", "IMG_1.JPG", taken=TAKEN)
    manifest.write_tree(tmp_path / "tree")

    with open_takeout(tmp_path / "tree") as takeout:
        assert takeout.parts == ()
        (entry,) = [e for e in takeout.entries if e.path.endswith(".JPG")]
        assert entry.part is None
        assert entry.crc32 is None
        assert takeout.original(entry, tmp_path / "work") == tmp_path / "tree" / entry.path
        assert takeout.read(entry.path)[:2] == b"\xff\xd8"


def test_a_folder_without_a_takeout_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()

    with pytest.raises(TakeoutError, match="no takeout"):
        open_takeout(tmp_path / "empty")


def test_extract_writes_read_only_originals_with_the_zip_times(tmp_path: Path) -> None:
    manifest = small_takeout(tmp_path / "in")
    dest = tmp_path / "work" / "extracted"

    with open_takeout(tmp_path / "in") as takeout:
        written = sum(takeout.extract(part.name, dest) for part in takeout.parts)
        entry = takeout.entries[0]

    assert written == len(takeout.entries)
    original = dest / entry.path
    assert original.exists()
    assert stat.S_IMODE(original.stat().st_mode) == 0o444
    assert datetime.fromtimestamp(original.stat().st_mtime) == datetime(2026, 10, 20, 8, 42, 20)
    assert not (dest / "Takeout" / "Maps (your places)").exists()
    assert (dest / PHOTOS_ROOT / "Été 2019" / "metadata.json").exists()
    assert {p.name for p in manifest.zips} == {p.name for p in (tmp_path / "in").iterdir()}


def test_extracted_bytes_match_the_zip(tmp_path: Path) -> None:
    small_takeout(tmp_path / "in")
    dest = tmp_path / "x"

    with open_takeout(tmp_path / "in") as takeout:
        for part in takeout.parts:
            takeout.extract(part.name, dest)
        for entry in takeout.entries:
            assert (dest / entry.path).read_bytes() == takeout.read(entry.path)


def test_extract_is_idempotent_and_never_touches_the_zips(tmp_path: Path) -> None:
    small_takeout(tmp_path / "in")
    dest = tmp_path / "x"
    zips_before = {p.name: digest(p) for p in (tmp_path / "in").iterdir()}

    with open_takeout(tmp_path / "in") as takeout:
        for part in takeout.parts:
            takeout.extract(part.name, dest)
        before = {p: p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file()}
        again = sum(takeout.extract(part.name, dest) for part in takeout.parts)

    assert again == 0
    assert {p: p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file()} == before
    assert {p.name: digest(p) for p in (tmp_path / "in").iterdir()} == zips_before


def test_extract_refuses_to_overwrite_a_different_original(tmp_path: Path) -> None:
    small_takeout(tmp_path / "in", parts=1)
    dest = tmp_path / "x"
    with open_takeout(tmp_path / "in") as takeout:
        takeout.extract(takeout.parts[0].name, dest)
        victim = dest / takeout.entries[0].path
        victim.chmod(0o644)
        victim.write_bytes(b"tampered")

        with pytest.raises(TakeoutError, match="differs"):
            takeout.extract(takeout.parts[0].name, dest)

    assert victim.read_bytes() == b"tampered"


@pytest.mark.parametrize(
    "evil", ["Takeout/Google Photos/../../../escape.jpg", "/Takeout/Google Photos/abs.jpg"]
)
def test_zip_entries_escaping_the_destination_are_rejected(tmp_path: Path, evil: str) -> None:
    (tmp_path / "in").mkdir()
    with zipfile.ZipFile(tmp_path / "in" / "takeout-20261020T000000Z-001.zip", "w") as bundle:
        bundle.writestr(evil, b"x")

    with pytest.raises(TakeoutError, match="unsafe"):
        open_takeout(tmp_path / "in")


def test_the_same_entry_in_two_parts_must_be_identical(tmp_path: Path) -> None:
    (tmp_path / "in").mkdir()
    for number, payload in ((1, b"one"), (2, b"two")):
        with zipfile.ZipFile(
            tmp_path / "in" / f"takeout-20261020T000000Z-00{number}.zip", "w"
        ) as z:
            z.writestr("Takeout/Google Photos/Photos from 2019/IMG_1.JPG", payload)

    with pytest.raises(TakeoutError, match="different"):
        open_takeout(tmp_path / "in")


def test_unknown_part_names_are_rejected(tmp_path: Path) -> None:
    small_takeout(tmp_path / "in")

    with open_takeout(tmp_path / "in") as takeout, pytest.raises(TakeoutError, match="no part"):
        takeout.extract("nope.zip", tmp_path / "x")


def test_reading_an_unknown_entry_fails(tmp_path: Path) -> None:
    small_takeout(tmp_path / "in")

    with open_takeout(tmp_path / "in") as takeout, pytest.raises(KeyError):
        takeout.read("Takeout/Google Photos/nope.jpg")


def test_directory_entries_and_identical_repeats_across_parts_are_fine(tmp_path: Path) -> None:
    (tmp_path / "in").mkdir()
    for number in (1, 2):
        with zipfile.ZipFile(
            tmp_path / "in" / f"takeout-20261020T000000Z-00{number}.zip", "w"
        ) as z:
            z.writestr("Takeout/Google Photos/Photos from 2019/", b"")
            z.writestr("Takeout/Google Photos/Photos from 2019/IMG_1.JPG", b"same")

    with open_takeout(tmp_path / "in") as takeout:
        assert [(e.path, e.part) for e in takeout.entries] == [
            ("Takeout/Google Photos/Photos from 2019/IMG_1.JPG", "takeout-20261020T000000Z-001.zip")
        ]


def test_an_extracted_tree_ignores_files_outside_google_photos(tmp_path: Path) -> None:
    b = TakeoutBuilder(seed=3)
    b.photo("Photos from 2019", "IMG_1.JPG", taken=TAKEN)
    b.other_product("Maps (your places)/Saved Places.json", b"{}")
    b.write_tree(tmp_path / "tree")

    with open_takeout(tmp_path / "tree") as takeout:
        assert takeout.ignored == 2


def test_locators_name_the_part_and_member_relative_to_the_takeout(tmp_path: Path) -> None:
    manifest = small_takeout(tmp_path / "in")

    with open_takeout(tmp_path / "in") as takeout:
        entry = next(e for e in takeout.entries if e.path.endswith("IMG_1.JPG"))
        assert takeout.locator(entry) == f"{entry.part}::{entry.path}"
        assert takeout.entry(takeout.locator(entry)) == entry
        assert manifest.parts[entry.path] >= 1


def test_tree_locators_are_plain_relative_paths(tmp_path: Path) -> None:
    b = TakeoutBuilder(seed=3)
    b.photo("Photos from 2019", "IMG_1.JPG", taken=TAKEN)
    b.write_tree(tmp_path / "tree")

    with open_takeout(tmp_path / "tree") as takeout:
        (entry,) = [e for e in takeout.entries if e.path.endswith(".JPG")]
        assert takeout.locator(entry) == entry.path
        assert takeout.entry(entry.path) == entry


def test_local_copy_streams_a_zip_entry_to_a_temporary_file(tmp_path: Path) -> None:
    small_takeout(tmp_path / "in")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with open_takeout(tmp_path / "in") as takeout:
        entry = next(e for e in takeout.entries if e.path.endswith("IMG_1.JPG"))
        expected = takeout.read(entry.path)
        with takeout.local_copy(entry, scratch) as (path, sha256):
            assert path.read_bytes() == expected
            assert path.suffix == ".JPG"
            assert sha256 == hashlib.sha256(expected).hexdigest()
            assert path.is_relative_to(scratch)

    assert list(scratch.iterdir()) == []


def test_local_copy_of_a_tree_entry_is_the_original_itself(tmp_path: Path) -> None:
    b = TakeoutBuilder(seed=3)
    b.photo("Photos from 2019", "IMG_1.JPG", taken=TAKEN)
    b.write_tree(tmp_path / "tree")

    with open_takeout(tmp_path / "tree") as takeout:
        (entry,) = [e for e in takeout.entries if e.path.endswith(".JPG")]
        with takeout.local_copy(entry, tmp_path) as (path, sha256):
            assert path == tmp_path / "tree" / entry.path
            assert sha256 == digest(path)


def test_unknown_locators_raise(tmp_path: Path) -> None:
    small_takeout(tmp_path / "in")

    with open_takeout(tmp_path / "in") as takeout, pytest.raises(KeyError):
        takeout.entry("nope.zip::Takeout/Google Photos/x.jpg")


def test_reading_an_unknown_tree_entry_fails(tmp_path: Path) -> None:
    b = TakeoutBuilder(seed=3)
    b.photo("Photos from 2019", "IMG_1.JPG", taken=TAKEN)
    b.write_tree(tmp_path / "tree")

    with open_takeout(tmp_path / "tree") as takeout, pytest.raises(KeyError):
        takeout.read("Takeout/Google Photos/Photos from 2019/nope.jpg")


def write_archive_parts(dest: Path, parts: dict[str, bytes]) -> None:
    """``parts`` maps a zip name to the bytes of Photos from 2019/IMG_1.JPG in it."""
    dest.mkdir(exist_ok=True)
    for name, payload in parts.items():
        with zipfile.ZipFile(dest / name, "w") as z:
            z.writestr("Takeout/Google Photos/Photos from 2019/IMG_1.JPG", payload)


SAME_NAME = "Takeout/Google Photos/Photos from 2019/IMG_1.JPG"


def test_the_same_path_in_two_archives_is_two_entries(tmp_path: Path) -> None:
    # A big export comes as several archives (takeout-<stamp>-<archive>-<part>.zip). Google
    # numbers clashing names within one archive but not across archives.
    write_archive_parts(
        tmp_path / "in",
        {
            "takeout-20261021T144622Z-1-001.zip": b"one",
            "takeout-20261021T144622Z-2-001.zip": b"two",
        },
    )

    with open_takeout(tmp_path / "in") as takeout:
        first, second = sorted(takeout.entries, key=lambda e: e.archive or "")
        assert (first.path, first.archive, first.key) == (SAME_NAME, "1", SAME_NAME)
        assert (second.path, second.archive) == (SAME_NAME, "2")
        assert second.key == "Takeout-2/Google Photos/Photos from 2019/IMG_1.JPG"
        assert takeout.read(first.key) == b"one"
        assert takeout.read(second.key) == b"two"


def test_parts_of_one_archive_must_still_agree(tmp_path: Path) -> None:
    write_archive_parts(
        tmp_path / "in",
        {
            "takeout-20261021T144622Z-1-001.zip": b"one",
            "takeout-20261021T144622Z-1-002.zip": b"two",
        },
    )

    with pytest.raises(TakeoutError, match="different"):
        open_takeout(tmp_path / "in")


def test_entries_of_unnumbered_parts_and_trees_have_no_archive(tmp_path: Path) -> None:
    small_takeout(tmp_path / "in")

    with open_takeout(tmp_path / "in") as takeout:
        assert {(e.archive, e.key == e.path) for e in takeout.entries} == {(None, True)}


def test_same_named_entries_of_two_archives_are_copied_and_extracted_apart(
    tmp_path: Path,
) -> None:
    write_archive_parts(
        tmp_path / "in",
        {
            "takeout-20261021T144622Z-1-001.zip": b"one",
            "takeout-20261021T144622Z-2-001.zip": b"two",
        },
    )

    (tmp_path / "scratch").mkdir()
    with open_takeout(tmp_path / "in") as takeout:
        for entry in takeout.entries:
            with takeout.local_copy(entry, tmp_path / "scratch") as (copy, _):
                assert copy.read_bytes() == {"1": b"one", "2": b"two"}[entry.archive or ""]
            assert entry.part is not None
            takeout.extract(entry.part, tmp_path / "work")
            original = takeout.original(entry, tmp_path / "work")
            assert original == tmp_path / "work" / entry.key
            assert original.read_bytes() == {"1": b"one", "2": b"two"}[entry.archive or ""]
