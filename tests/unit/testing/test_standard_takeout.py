"""The standard synthetic Takeout covers every quirk listed in M1-02 (CLA-74)."""

import hashlib
import json
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import PurePosixPath

import pytest

from photomigrate.testing.takeout import (
    PHOTOS_ROOT,
    SUPPLEMENTAL_SUFFIX,
    Item,
    TakeoutManifest,
    standard_takeout,
)


def read(manifest: TakeoutManifest, path: str) -> bytes:
    for archive in manifest.zips:
        with zipfile.ZipFile(archive) as bundle:
            if path in bundle.namelist():
                return bundle.read(path)
    raise KeyError(path)


def sidecar(manifest: TakeoutManifest, item: Item) -> dict[str, object]:
    path = manifest.metadata_sidecar(item)
    assert path is not None
    data: dict[str, object] = json.loads(read(manifest, path))
    return data


def named(manifest: TakeoutManifest, name: str) -> Item:
    (item,) = [i for i in manifest.items if i.name == name and i.copy_of is None]
    return item


def folders(manifest: TakeoutManifest) -> set[str]:
    return {item.folder for item in manifest.items}


def test_year_folders_and_albums_with_metadata(standard_takeout_zips: TakeoutManifest) -> None:
    m = standard_takeout_zips

    assert {"Photos from 2019", "Photos from 2015", "Photos from 1970"} <= folders(m)
    assert {"Été 2019", "Best of", "Family", "Exports"} <= set(m.albums)
    for album in m.albums:
        meta = json.loads(read(m, f"{PHOTOS_ROOT}/{album}/metadata.json"))
        assert meta["title"] == album
    assert json.loads(read(m, f"{PHOTOS_ROOT}/Été 2019/metadata.json"))["sharedAlbumComments"]


def test_sidecars_carry_dates_location_description_and_favourite(
    standard_takeout_zips: TakeoutManifest,
) -> None:
    beach = named(standard_takeout_zips, "IMG_1001.JPG")

    data = sidecar(standard_takeout_zips, beach)

    assert {"photoTakenTime", "creationTime", "geoData", "description"} <= set(data)
    assert data["favorited"] is True
    assert data["description"] == "Plage à Nice 🏖️"
    assert data["geoData"]["latitude"] != 0  # type: ignore[index]


def test_both_sidecar_naming_styles(standard_takeout_zips: TakeoutManifest) -> None:
    m = standard_takeout_zips

    for item in m.with_quirk("supplemental-sidecar"):
        assert item.sidecar
        assert ".supplemental-" in item.sidecar or "truncated-sidecar" in item.quirks
    legacy = m.with_quirk("legacy-sidecar")
    assert legacy
    assert all(i.sidecar and SUPPLEMENTAL_SUFFIX not in i.sidecar for i in legacy)


def test_truncated_names_and_differently_truncated_sidecars(
    standard_takeout_zips: TakeoutManifest,
) -> None:
    truncated = standard_takeout_zips.with_quirk("truncated-name")

    assert len(truncated) >= 2
    assert {"legacy-sidecar", "supplemental-sidecar"} <= set().union(*(i.quirks for i in truncated))
    for item in truncated:
        assert len(item.name) == 47
        assert item.sidecar is not None
        sidecar_name = PurePosixPath(item.sidecar).name
        assert len(sidecar_name) == 51
        assert not sidecar_name.startswith(item.name + ".")
        assert len(str(sidecar(standard_takeout_zips, item)["title"])) > 47


def test_duplicate_index_quirk_in_both_styles(standard_takeout_zips: TakeoutManifest) -> None:
    dups = standard_takeout_zips.with_quirk("duplicate-index")

    names = {(i.name, PurePosixPath(i.sidecar or "").name) for i in dups}
    assert ("IMG_1004(1).JPG", "IMG_1004.JPG.supplemental-metadata(1).json") in names
    assert ("IMG_0500(1).jpg", "IMG_0500.jpg(1).json") in names


def test_edited_copies_including_localised_variants(standard_takeout_zips: TakeoutManifest) -> None:
    m = standard_takeout_zips
    edits = m.with_quirk("edited")

    names = {e.name for e in edits}
    assert {
        "IMG_1001-edited.JPG",
        "IMG_1005-edited.jpg",
        "IMG_1004-edited(1).JPG",
        "IMG_0500-modifié.jpg",
        "IMG_0500-bearbeitet(1).jpg",
        "IMG_1003-SMILE-edited.jpg",
    } <= names
    assert m.with_quirk("edited-extension-case")
    assert m.with_quirk("truncated-edit-suffix")
    for edit in edits:
        assert edit.sidecar is None
        original = m.item(edit.edit_of or "")
        assert m.metadata_sidecar(edit) == original.sidecar
        assert sidecar(m, edit)["title"]


def test_live_photos_are_split_into_still_and_motion(
    standard_takeout_zips: TakeoutManifest,
) -> None:
    m = standard_takeout_zips
    stills = [i for i in m.with_quirk("live-photo") if i.kind == "photo"]

    pairs = {
        (PurePosixPath(s.path).suffix, PurePosixPath(s.live_partner or "").suffix) for s in stills
    }
    assert pairs == {(".HEIC", ".MOV"), (".JPG", ".MP4")}
    for still in stills:
        motion = m.item(still.live_partner or "")
        assert motion.kind == "video"
        assert motion.live_partner == still.path
        assert PurePosixPath(motion.path).stem == PurePosixPath(still.path).stem


def test_missing_sidecars_for_a_photo_and_a_video(standard_takeout_zips: TakeoutManifest) -> None:
    missing = [
        i
        for i in standard_takeout_zips.with_quirk("missing-sidecar")
        if "live-photo" not in i.quirks
    ]

    assert {i.kind for i in missing} == {"photo", "video"}


def test_placeholder_future_and_upload_dates(standard_takeout_zips: TakeoutManifest) -> None:
    m = standard_takeout_zips
    placeholder_years = {
        (i.embedded_taken or i.taken).year for i in m.with_quirk("placeholder-date")
    }

    assert placeholder_years == {1970, 1980, 2000}
    epoch = named(m, "IMG_0001.JPG")
    assert sidecar(m, epoch)["photoTakenTime"]["timestamp"] == "0"  # type: ignore[index]
    assert m.with_quirk("future-date")
    upload = named(m, "IMG_20120304_101112.jpg")
    assert "upload-date-as-taken" in upload.quirks
    assert upload.taken.year == 2019
    ordinary = named(m, "IMG_1002.JPG")
    assert ordinary.created > ordinary.taken


def test_same_item_in_three_albums_is_byte_identical(
    standard_takeout_zips: TakeoutManifest,
) -> None:
    m = standard_takeout_zips
    beach = named(m, "IMG_1001.JPG")

    copies = [i for i in m.items if i.copy_of == beach.path]

    assert {c.folder for c in copies} == {"Été 2019", "Best of", "Family"}
    for copy in copies:
        assert read(m, copy.path) == read(m, beach.path)
        assert copy.sidecar is not None


def test_near_duplicates_of_each_kind(standard_takeout_zips: TakeoutManifest) -> None:
    kinds = {i.near_duplicate_kind for i in standard_takeout_zips.with_quirk("near-duplicate")}

    assert kinds == {"resized", "recompressed", "heic-vs-jpeg"}


def test_videos_are_real_clips(
    standard_takeout_zips: TakeoutManifest, tmp_path_factory: pytest.TempPathFactory
) -> None:
    scratch = tmp_path_factory.mktemp("videos")
    videos = [i for i in standard_takeout_zips.items if i.kind == "video" and i.copy_of is None]

    assert len(videos) >= 5
    for video in videos:
        clip = scratch / video.name
        clip.write_bytes(read(standard_takeout_zips, video.path))
        duration = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(clip),
            ],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
        assert float(duration) > 0.5


def test_output_is_split_across_zip_parts(standard_takeout_zips: TakeoutManifest) -> None:
    m = standard_takeout_zips

    assert len(m.zips) == 3
    assert {m.parts[path] for path in m.files} == {1, 2, 3}
    assert any(m.parts[i.path] != m.parts[i.sidecar] for i in m.items if i.sidecar)


def test_google_creations(standard_takeout_zips: TakeoutManifest) -> None:
    kinds = {i.google_creation for i in standard_takeout_zips.with_quirk("google-creation")}

    assert kinds == {"MOTION", "MIX", "SMILE"}


def test_people_unicode_and_other_products(standard_takeout_zips: TakeoutManifest) -> None:
    m = standard_takeout_zips

    assert m.with_quirk("people")
    assert m.with_quirk("unicode-description")
    assert any(not path.startswith(PHOTOS_ROOT) and path.endswith(".json") for path in m.files)


def accounted_for(manifest: TakeoutManifest) -> Iterator[str]:
    for item in manifest.items:
        yield item.path
        if item.sidecar:
            yield item.sidecar
    for album in manifest.albums:
        yield f"{PHOTOS_ROOT}/{album}/metadata.json"


def test_every_photos_file_is_a_known_media_sidecar_or_album_file(
    standard_takeout_zips: TakeoutManifest,
) -> None:
    m = standard_takeout_zips

    photos_files = {path for path in m.files if path.startswith(PHOTOS_ROOT)}

    assert photos_files == set(accounted_for(m))


def test_fixtures_are_small(standard_takeout_zips: TakeoutManifest) -> None:
    assert sum(z.stat().st_size for z in standard_takeout_zips.zips) < 5_000_000


def test_standard_takeout_is_reproducible() -> None:
    def digest(files: dict[str, bytes]) -> dict[str, str]:
        return {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}

    assert digest(standard_takeout(seed=0).files()) == digest(standard_takeout(seed=0).files())
