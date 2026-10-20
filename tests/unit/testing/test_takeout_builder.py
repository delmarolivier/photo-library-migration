"""TakeoutBuilder assembles Takeout exports and records the truth about every file."""

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import ExifTags, Image

from photomigrate.testing.takeout import (
    FIXTURE_NOW,
    PHOTOS_ROOT,
    Geo,
    TakeoutBuilder,
    TakeoutManifest,
)

TAKEN = datetime(2019, 7, 1, 12, 34, 56, tzinfo=timezone(timedelta(hours=2)))
NICE = Geo(43.6970333, 7.266)


def open_image(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


@pytest.fixture
def builder() -> TakeoutBuilder:
    return TakeoutBuilder(seed=1)


def files(builder: TakeoutBuilder) -> dict[str, bytes]:
    return builder.files()


def sidecar_of(builder: TakeoutBuilder, path: str | None) -> dict[str, object]:
    assert path is not None
    data: dict[str, object] = json.loads(files(builder)[path])
    return data


def test_photo_writes_media_and_supplemental_sidecar(builder: TakeoutBuilder) -> None:
    item = builder.photo("Photos from 2019", "IMG_1234.JPG", taken=TAKEN, geo=NICE)

    assert item.path == f"{PHOTOS_ROOT}/Photos from 2019/IMG_1234.JPG"
    assert item.sidecar == f"{PHOTOS_ROOT}/Photos from 2019/IMG_1234.JPG.supplemental-metadata.json"
    assert item.kind == "photo"
    assert item.folder == "Photos from 2019"
    assert item.name == "IMG_1234.JPG"
    data = sidecar_of(builder, item.sidecar)
    assert data["title"] == "IMG_1234.JPG"
    assert data["photoTakenTime"] == {
        "timestamp": "1561977296",
        "formatted": "1 Jul 2019, 10:34:56 UTC",
    }
    assert data["geoData"]["latitude"] == pytest.approx(43.6970333)  # type: ignore[index]
    assert "supplemental-sidecar" in item.quirks


def test_photo_embeds_camera_exif_by_default(builder: TakeoutBuilder) -> None:
    item = builder.photo(
        "Photos from 2019", "IMG_1234.JPG", taken=TAKEN, geo=NICE, geo_in_exif=True
    )

    exif = open_image(files(builder)[item.path]).getexif()

    assert exif[ExifTags.Base.Model] == "iPhone 8"
    assert exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] == "2019:07:01 12:34:56"
    assert ExifTags.IFD.GPSInfo in exif
    assert item.embedded_taken == TAKEN
    assert "geo-in-exif" in item.quirks


def test_upload_time_defaults_to_after_the_capture(builder: TakeoutBuilder) -> None:
    item = builder.photo("Photos from 2019", "IMG_1234.JPG", taken=TAKEN)

    assert TAKEN < item.created < FIXTURE_NOW
    assert "upload-date-as-taken" not in item.quirks


def test_upload_date_used_as_capture_date(builder: TakeoutBuilder) -> None:
    item = builder.photo(
        "Photos from 2019", "IMG_1234.JPG", taken=TAKEN, created=TAKEN, camera=None, exif_taken=None
    )

    assert item.created == item.taken
    assert {"upload-date-as-taken", "no-exif", "no-exif-date"} <= item.quirks
    assert len(open_image(files(builder)[item.path]).getexif()) == 0


def test_photo_without_sidecar(builder: TakeoutBuilder) -> None:
    item = builder.photo("Photos from 2019", "IMG_1234.JPG", taken=TAKEN, sidecar=None)

    assert item.sidecar is None
    assert "missing-sidecar" in item.quirks
    assert list(files(builder)) == ["Takeout/archive_browser.html", item.path]


def test_legacy_sidecar_with_duplicate_index(builder: TakeoutBuilder) -> None:
    item = builder.photo("Photos from 2015", "IMG_0500.jpg", taken=TAKEN, sidecar="legacy", index=1)

    assert item.name == "IMG_0500(1).jpg"
    assert item.sidecar == f"{PHOTOS_ROOT}/Photos from 2015/IMG_0500.jpg(1).json"
    assert sidecar_of(builder, item.sidecar)["title"] == "IMG_0500.jpg"
    assert {"duplicate-index", "legacy-sidecar"} <= item.quirks


def test_long_names_are_truncated_but_the_sidecar_title_keeps_the_full_name(
    builder: TakeoutBuilder,
) -> None:
    long_name = "Screenshot_20191105-123456_A_Rather_Long_Application_Name.jpg"

    item = builder.photo("Photos from 2019", long_name, taken=TAKEN, sidecar="legacy")

    assert len(item.name) == 47
    assert item.sidecar is not None
    assert item.sidecar.endswith(".jp.json")
    assert sidecar_of(builder, item.sidecar)["title"] == long_name
    assert {"truncated-name", "truncated-sidecar"} <= item.quirks


@pytest.mark.parametrize(
    ("taken", "quirk"),
    [
        (datetime(1970, 1, 1, tzinfo=UTC), "placeholder-date"),
        (datetime(1980, 1, 1, tzinfo=UTC), "placeholder-date"),
        (datetime(2000, 1, 1, tzinfo=UTC), "placeholder-date"),
        (datetime(2037, 6, 1, tzinfo=UTC), "future-date"),
    ],
)
def test_suspicious_dates_are_tagged(builder: TakeoutBuilder, taken: datetime, quirk: str) -> None:
    item = builder.photo("Photos from 2019", "IMG_1234.JPG", taken=taken)

    assert quirk in item.quirks
    assert item.created < FIXTURE_NOW


def test_placeholder_epoch_date_is_timestamp_zero(builder: TakeoutBuilder) -> None:
    item = builder.photo("Photos from 1970", "IMG_1234.JPG", taken=datetime(1970, 1, 1, tzinfo=UTC))

    assert sidecar_of(builder, item.sidecar)["photoTakenTime"]["timestamp"] == "0"  # type: ignore[index]


def test_camera_clock_placeholder_in_exif_only(builder: TakeoutBuilder) -> None:
    item = builder.photo(
        "Photos from 2019", "IMG_1234.JPG", taken=TAKEN, exif_taken=datetime(2000, 1, 1)
    )

    exif = open_image(files(builder)[item.path]).getexif().get_ifd(ExifTags.IFD.Exif)
    assert exif[ExifTags.Base.DateTimeOriginal] == "2000:01:01 00:00:00"
    assert "placeholder-date" in item.quirks


def test_heic_photos_follow_the_extension(builder: TakeoutBuilder) -> None:
    item = builder.photo("Photos from 2019", "IMG_1234.HEIC", taken=TAKEN)

    assert open_image(files(builder)[item.path]).format == "HEIF"


def test_unicode_description_people_and_favourite(builder: TakeoutBuilder) -> None:
    item = builder.photo(
        "Photos from 2019",
        "IMG_1234.JPG",
        taken=TAKEN,
        description="東京タワー 🗼",
        favorited=True,
        people=("Alice Example",),
    )

    data = sidecar_of(builder, item.sidecar)
    assert data["description"] == "東京タワー 🗼"
    assert data["favorited"] is True
    assert data["people"] == [{"name": "Alice Example"}]
    assert item.people == ("Alice Example",)
    assert {"unicode-description", "favourite", "people"} <= item.quirks


def test_same_path_twice_is_an_error(builder: TakeoutBuilder) -> None:
    builder.photo("Photos from 2019", "IMG_1234.JPG", taken=TAKEN)

    with pytest.raises(ValueError, match="already"):
        builder.photo("Photos from 2019", "IMG_1234.JPG", taken=TAKEN)


@pytest.mark.parametrize(("name", "codec_box"), [("IMG_1010.MOV", b"qt  "), ("VID_1.mp4", b"isom")])
def test_videos_are_real_clips_in_the_right_container(
    builder: TakeoutBuilder, name: str, codec_box: bytes
) -> None:
    item = builder.video("Photos from 2019", name, taken=TAKEN)

    data = files(builder)[item.path]
    assert item.kind == "video"
    assert data[4:8] == b"ftyp"
    assert data[8:12] == codec_box
    assert item.sidecar is not None


def test_copy_to_another_album_is_byte_identical_with_its_own_sidecar(
    builder: TakeoutBuilder,
) -> None:
    original = builder.photo("Photos from 2019", "IMG_1234.JPG", taken=TAKEN)

    copy = builder.copy_to(original, "Été 2019")

    assert copy.path == f"{PHOTOS_ROOT}/Été 2019/IMG_1234.JPG"
    assert copy.copy_of == original.path
    assert files(builder)[copy.path] == files(builder)[original.path]
    assert files(builder)[copy.sidecar or ""] == files(builder)[original.sidecar or ""]
    assert builder.copy_to(copy, "Best of").copy_of == original.path


def test_edited_copy_has_no_sidecar_and_looks_like_the_original(builder: TakeoutBuilder) -> None:
    original = builder.photo("Photos from 2019", "IMG_1234.JPG", taken=TAKEN)

    edit = builder.edited(original, extension=".jpg")

    assert edit.name == "IMG_1234-edited.jpg"
    assert edit.sidecar is None
    assert edit.edit_of == original.path
    assert edit.taken == original.taken
    assert {"edited", "edited-extension-case", "uses-original-sidecar"} <= edit.quirks
    before = open_image(files(builder)[original.path])
    after = open_image(files(builder)[edit.path])
    assert before.size == after.size
    assert before.tobytes() != after.tobytes()


def test_localised_edited_copy_of_a_duplicate(builder: TakeoutBuilder) -> None:
    original = builder.photo("Photos from 2015", "IMG_0500.jpg", taken=TAKEN, index=1)

    edit = builder.edited(original, lang="fr")

    assert edit.name == "IMG_0500-modifié(1).jpg"
    assert "edited-fr" in edit.quirks


def test_manifest_points_edits_at_the_original_sidecar(builder: TakeoutBuilder) -> None:
    original = builder.photo("Photos from 2019", "IMG_1234.JPG", taken=TAKEN)
    edit = builder.edited(original)

    manifest = builder.manifest()

    assert manifest.metadata_sidecar(edit) == original.sidecar
    assert manifest.metadata_sidecar(original) == original.sidecar


def test_live_photo_is_a_still_plus_a_motion_clip(builder: TakeoutBuilder) -> None:
    still, motion = builder.live_photo("Photos from 2019", "IMG_2001", taken=TAKEN)

    assert (still.name, motion.name) == ("IMG_2001.HEIC", "IMG_2001.MOV")
    assert still.live_partner == motion.path
    assert motion.live_partner == still.path
    assert motion.kind == "video"
    assert motion.sidecar is None
    assert "live-photo" in still.quirks
    manifest = builder.manifest()
    assert manifest.item(still.path).live_partner == motion.path


def test_live_photo_variant_with_jpeg_mp4_and_both_sidecars(builder: TakeoutBuilder) -> None:
    still, motion = builder.live_photo(
        "Photos from 2019",
        "IMG_2002",
        taken=TAKEN,
        still_ext=".JPG",
        motion_ext=".MP4",
        motion_sidecar=True,
        sidecar="legacy",
    )

    assert motion.sidecar == f"{PHOTOS_ROOT}/Photos from 2019/IMG_2002.MP4.json"
    assert still.sidecar == f"{PHOTOS_ROOT}/Photos from 2019/IMG_2002.JPG.json"


def test_near_duplicates(builder: TakeoutBuilder) -> None:
    original = builder.photo("Photos from 2019", "IMG_1234.JPG", taken=TAKEN)
    resized = builder.near_duplicate(
        original, "Photos from 2019", "IMG-20190702-WA0003.jpg", kind="resized", strip_exif=True
    )
    recompressed = builder.near_duplicate(
        original, "Photos from 2019", "received_1234.jpeg", kind="recompressed"
    )
    heic = builder.near_duplicate(original, "Exports", "IMG_1234.HEIC", kind="heic-vs-jpeg")

    data = files(builder)
    assert open_image(data[resized.path]).size == (160, 120)
    assert len(open_image(data[resized.path]).getexif()) == 0
    assert len(data[recompressed.path]) < len(data[original.path])
    assert open_image(data[heic.path]).format == "HEIF"
    assert {i.near_duplicate_of for i in (resized, recompressed, heic)} == {original.path}
    assert [i.near_duplicate_kind for i in (resized, recompressed, heic)] == [
        "resized",
        "recompressed",
        "heic-vs-jpeg",
    ]


def test_heic_vs_jpeg_needs_a_different_format(builder: TakeoutBuilder) -> None:
    original = builder.photo("Photos from 2019", "IMG_1234.JPG", taken=TAKEN)

    with pytest.raises(ValueError, match="format"):
        builder.near_duplicate(original, "Exports", "IMG_1234.jpg", kind="heic-vs-jpeg")


@pytest.mark.parametrize(
    ("kind", "name", "fmt"),
    [
        ("MOTION", "IMG_1234-MOTION.gif", "GIF"),
        ("MIX", "IMG_1234-MIX.jpg", "JPEG"),
        ("SMILE", "IMG_1234-SMILE.jpg", "JPEG"),
    ],
)
def test_google_creations(builder: TakeoutBuilder, kind: str, name: str, fmt: str) -> None:
    original = builder.photo("Photos from 2019", "IMG_1234.JPG", taken=TAKEN)

    creation = builder.google_creation(original, kind)  # type: ignore[arg-type]

    assert creation.name == name
    assert creation.google_creation == kind
    assert open_image(files(builder)[creation.path]).format == fmt
    assert sidecar_of(builder, creation.sidecar)["googlePhotosOrigin"] == {
        "composition": {"type": "AUTO"}
    }


def test_album_metadata_and_other_products(builder: TakeoutBuilder) -> None:
    builder.album("Été 2019", description="Vacances 🌞")
    builder.other_product("Maps (your places)/Saved Places.json", b"{}")

    data = files(builder)

    assert json.loads(data[f"{PHOTOS_ROOT}/Été 2019/metadata.json"]) == {
        "title": "Été 2019",
        "description": "Vacances 🌞",
    }
    assert data["Takeout/Maps (your places)/Saved Places.json"] == b"{}"
    assert builder.manifest().albums == ("Été 2019",)


def built(builder: TakeoutBuilder) -> TakeoutBuilder:
    original = builder.photo("Photos from 2019", "IMG_1234.JPG", taken=TAKEN)
    builder.edited(original)
    builder.video("Photos from 2019", "IMG_1010.MOV", taken=TAKEN)
    builder.album("Été 2019")
    builder.copy_to(original, "Été 2019")
    return builder


def test_write_tree_writes_every_file(builder: TakeoutBuilder, tmp_path: Path) -> None:
    manifest = built(builder).write_tree(tmp_path)

    assert manifest.root == tmp_path
    for path, data in builder.files().items():
        assert (tmp_path / path).read_bytes() == data


def test_write_zips_splits_files_across_parts(builder: TakeoutBuilder, tmp_path: Path) -> None:
    manifest = built(builder).write_zips(tmp_path, parts=3)

    assert [z.name for z in manifest.zips] == [
        "takeout-20261020T084220Z-001.zip",
        "takeout-20261020T084220Z-002.zip",
        "takeout-20261020T084220Z-003.zip",
    ]
    seen: dict[str, int] = {}
    for number, archive in enumerate(manifest.zips, start=1):
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                assert name not in seen
                seen[name] = number
                assert zf.read(name) == builder.files()[name]
    assert seen == manifest.parts
    assert set(seen) == set(builder.files())
    assert seen["Takeout/archive_browser.html"] == 1
    assert any(seen[item.path] != seen[item.sidecar] for item in manifest.items if item.sidecar), (
        "some media should land in a different part from its sidecar"
    )


def test_zips_are_byte_for_byte_reproducible(tmp_path: Path) -> None:
    first = built(TakeoutBuilder(seed=1)).write_zips(tmp_path / "a", parts=2)
    second = built(TakeoutBuilder(seed=1)).write_zips(tmp_path / "b", parts=2)

    assert [z.read_bytes() for z in first.zips] == [z.read_bytes() for z in second.zips]


def test_different_seeds_give_different_pixels(tmp_path: Path) -> None:
    one = built(TakeoutBuilder(seed=1)).files()
    two = built(TakeoutBuilder(seed=2)).files()

    path = f"{PHOTOS_ROOT}/Photos from 2019/IMG_1234.JPG"
    assert one[path] != two[path]


def test_manifest_lookup_quirks_and_json(builder: TakeoutBuilder) -> None:
    built(builder)

    manifest = builder.manifest()

    item = manifest.item(f"{PHOTOS_ROOT}/Photos from 2019/IMG_1234.JPG")
    assert item.name == "IMG_1234.JPG"
    assert [i.name for i in manifest.with_quirk("edited")] == ["IMG_1234-edited.JPG"]
    with pytest.raises(KeyError):
        manifest.item("nope")
    decoded = json.loads(manifest.to_json())
    assert decoded["albums"] == ["Été 2019"]
    first = decoded["items"][0]
    assert first["path"] == item.path
    assert first["taken"] == "2019-07-01T12:34:56+02:00"
    assert sorted(first["quirks"]) == sorted(item.quirks)
    assert isinstance(manifest, TakeoutManifest)


def test_unknown_extensions_are_rejected(builder: TakeoutBuilder) -> None:
    with pytest.raises(ValueError, match="photo extension"):
        builder.photo("Photos from 2019", "notes.txt", taken=TAKEN)
    with pytest.raises(ValueError, match="video extension"):
        builder.video("Photos from 2019", "clip.avi", taken=TAKEN)


def test_copy_of_an_item_without_sidecar_has_none(builder: TakeoutBuilder) -> None:
    original = builder.photo("Photos from 2019", "IMG_1234.JPG", taken=TAKEN, sidecar=None)

    copy = builder.copy_to(original, "Family")

    assert copy.sidecar is None
    assert list(builder.files()) == ["Takeout/archive_browser.html", original.path, copy.path]
