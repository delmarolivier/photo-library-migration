"""The ingest stage end to end on the standard synthetic Takeout (CLA-76)."""

import hashlib
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.embedding import from_bytes
from photomigrate.faces import faces_from_json
from photomigrate.ingest.stage import IngestStage
from photomigrate.runner import RunResult, run_stage
from photomigrate.testing.embedder import ThumbnailEmbedder
from photomigrate.testing.faces import ONE_FACE, FixedFaces
from photomigrate.testing.takeout import TakeoutBuilder, TakeoutManifest, standard_takeout

ZONE = ZoneInfo("Europe/Paris")


def zip_hashes(manifest: TakeoutManifest) -> dict[str, str]:
    return {z.name: hashlib.sha256(z.read_bytes()).hexdigest() for z in manifest.zips}


def member_sha(manifest: TakeoutManifest, path: str) -> str:
    archive = manifest.zips[manifest.parts[path] - 1]
    with zipfile.ZipFile(archive) as bundle:
        return hashlib.sha256(bundle.read(path)).hexdigest()


def ingest(takeout: Path, tmp: Path, *, apply: bool) -> RunResult:
    with IngestStage(takeout, tmp / "work", zone=ZONE) as stage:
        return run_stage(
            stage, catalogue_path=tmp / "catalogue.sqlite", plan_dir=tmp / "plans", apply=apply
        )


@pytest.fixture(scope="module")
def applied(
    standard_takeout_zips: TakeoutManifest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[TakeoutManifest, Path, RunResult, dict[str, str]]]:
    tmp = tmp_path_factory.mktemp("ingest")
    before = zip_hashes(standard_takeout_zips)
    result = ingest(standard_takeout_zips.zips[0].parent, tmp, apply=True)
    return standard_takeout_zips, tmp, result, before


def items(tmp: Path) -> dict[str, CatalogueItem]:
    with Catalogue.open(tmp / "catalogue.sqlite") as catalogue:
        return {item.source_key: item for item in catalogue.items("google")}


def candidates(tmp: Path, key: str) -> dict[str, datetime]:
    with Catalogue.open(tmp / "catalogue.sqlite") as catalogue:
        item = catalogue.get_item("google", key)
        assert item is not None
        assert item.id is not None
        return catalogue.date_candidates(item.id)


def test_every_item_is_catalogued_with_its_true_sidecar_or_flagged(applied) -> None:  # type: ignore[no-untyped-def]
    manifest, tmp, _, _ = applied
    stored = items(tmp)

    assert set(stored) == {item.path for item in manifest.items if item.copy_of is None}
    for item in manifest.items:
        if item.copy_of is not None:
            continue
        row = stored[item.path]
        assert row.sidecar_path == manifest.metadata_sidecar(item), item.path
        if row.sidecar_path is None:
            assert "no-sidecar" in row.flags, item.path


def test_every_takeout_media_file_resolves_to_exactly_one_item(applied) -> None:  # type: ignore[no-untyped-def]
    manifest, tmp, _, _ = applied

    with Catalogue.open(tmp / "catalogue.sqlite") as catalogue:
        for item in manifest.items:
            owner = item.copy_of or item.path
            assert catalogue.item_for_key("google", item.path) == catalogue.item_for_key(
                "google", owner
            ), item.path


def test_originals_are_located_in_their_zip_part_and_hashed(applied) -> None:  # type: ignore[no-untyped-def]
    manifest, tmp, _, before = applied

    for key, row in items(tmp).items():
        part = manifest.zips[manifest.parts[key] - 1].name
        assert row.original_path == f"{part}::{key}"
        assert row.working_path is None
        assert row.sha256 == member_sha(manifest, key)
    assert zip_hashes(manifest) == before, "the zips are never modified"


def test_kinds_titles_captions_favourites_and_sizes(applied) -> None:  # type: ignore[no-untyped-def]
    manifest, tmp, _, _ = applied
    stored = items(tmp)

    beach = stored["Takeout/Google Photos/Photos from 2019/IMG_1001.JPG"]
    assert (beach.kind, beach.title, beach.favorite) == ("photo", "IMG_1001.JPG", True)
    assert beach.description == "Plage à Nice 🏖️"
    assert (beach.width, beach.height) == (320, 240)
    clip = stored["Takeout/Google Photos/Photos from 2019/VID_20190704_101010.mp4"]
    assert clip.kind == "video"
    assert clip.duration == pytest.approx(2.0, abs=0.1)
    long_name = next(r for r in stored.values() if "truncated-name" in next(
        i.quirks for i in manifest.items if i.path == r.source_key))  # fmt: skip
    assert long_name.title is not None
    assert len(long_name.title) > 47


def test_date_candidates_come_from_every_source(applied) -> None:  # type: ignore[no-untyped-def]
    manifest, tmp, _, _ = applied

    found = candidates(tmp, "Takeout/Google Photos/Photos from 2019/IMG_1001.JPG")
    beach = next(i for i in manifest.items if i.name == "IMG_1001.JPG" and i.copy_of is None)
    assert found["sidecar-taken"] == beach.taken
    assert found["sidecar-created"] == beach.created
    assert found["exif"] == beach.taken
    assert found["mtime"] == datetime(2026, 10, 20, 8, 42, 20)
    named = candidates(tmp, "Takeout/Google Photos/Photos from 2019/IMG_20120304_101112.jpg")
    assert named["filename"] == datetime(2012, 3, 4, 10, 11, 12)
    video = candidates(tmp, "Takeout/Google Photos/Photos from 2019/IMG_1010.MOV")
    assert video["quicktime"] == next(i.taken for i in manifest.items if i.name == "IMG_1010.MOV")


def plan_of(tmp: Path, key: str) -> dict[str, object]:
    plan: dict[str, object] = json.loads(items(tmp)[key].metadata_plan)
    return plan


def test_metadata_plans_follow_the_rules(applied) -> None:  # type: ignore[no-untyped-def]
    _manifest, tmp, _, _ = applied
    root = "Takeout/Google Photos"
    stored = items(tmp)

    # The camera date wins; GPS already in EXIF; the caption is new.
    beach = plan_of(tmp, f"{root}/Photos from 2019/IMG_1001.JPG")
    assert beach["taken"] is None
    assert beach["latitude"] is None
    assert beach["description"] == "Plage à Nice 🏖️"
    # No EXIF date (a stripped messaging forward): the sidecar date, in the home zone.
    forward = f"{root}/Photos from 2019/IMG-20190702-WA0003.jpg"
    assert plan_of(tmp, forward)["taken"] == "2019-07-02T08:00:00+02:00"
    assert {"date-from-sidecar", "offset-assumed"} <= stored[forward].flags
    # GPS only in the sidecar.
    tower = plan_of(tmp, f"{root}/Photos from 2019/IMG_1003.HEIC")
    assert tower["latitude"] == pytest.approx(35.6585805)
    # Suspicious dates are never written.
    for key, flag in (
        (f"{root}/Photos from 1970/IMG_0001.JPG", "sidecar-date-placeholder"),
        (f"{root}/Photos from 2019/IMG_20120304_101112.jpg", "sidecar-date-is-upload"),
    ):
        assert plan_of(tmp, key)["taken"] is None
        assert {flag, "date-unknown"} <= stored[key].flags


def test_flags_describe_matches_edits_and_google_creations(applied) -> None:  # type: ignore[no-untyped-def]
    _, tmp, _, _ = applied
    stored = items(tmp)
    root = "Takeout/Google Photos/Photos from 2019"

    assert "match-exact" in stored[f"{root}/IMG_1001.JPG"].flags
    assert {"edited", "match-edited-original"} <= stored[f"{root}/IMG_1001-edited.JPG"].flags
    assert "google-creation" in stored[f"{root}/IMG_1001-MOTION.gif"].flags
    assert "google-creation" in stored[f"{root}/IMG_1003-SMILE-edited.jpg"].flags
    assert "match-unmatched" in stored[f"{root}/IMG_1011.MOV"].flags


def test_re_running_ingest_changes_nothing(applied) -> None:  # type: ignore[no-untyped-def]
    manifest, tmp, first, _ = applied
    catalogue_before = (tmp / "catalogue.sqlite").read_bytes()

    again = ingest(manifest.zips[0].parent, tmp, apply=True)

    assert first.applied == len(manifest.items) + len(manifest.albums)
    assert again.applied == 0
    assert (tmp / "catalogue.sqlite").read_bytes() == catalogue_before


def test_a_dry_run_only_writes_the_plan(
    standard_takeout_zips: TakeoutManifest, tmp_path: Path
) -> None:
    result = ingest(standard_takeout_zips.zips[0].parent, tmp_path, apply=False)

    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == [result.plan_path.name]  # type: ignore[union-attr]
    assert result.plan.summary() == {
        "album": len(standard_takeout_zips.albums),
        "ingest": len(standard_takeout_zips.items),
    }


def test_orphan_sidecars_and_unknown_files_are_flagged(tmp_path: Path) -> None:
    b = TakeoutBuilder(seed=5)
    b.photo("Photos from 2019", "IMG_1.JPG", taken=datetime(2019, 7, 1, tzinfo=UTC))
    b.other_product("Google Photos/Photos from 2019/IMG_404.JPG.supplemental-metadata.json", b"{}")
    b.other_product("Google Photos/Photos from 2019/notes.txt", b"hello")
    b.other_product("Google Photos/Photos from 2019/broken.jpg.supplemental-metadata.json", b"{")
    b.photo("Photos from 2019", "broken.jpg", taken=datetime(2019, 7, 1, tzinfo=UTC), sidecar=None)
    b.write_zips(tmp_path / "in")

    result = ingest(tmp_path / "in", tmp_path, apply=True)

    kinds = {(a.kind, a.target.rsplit("/", 1)[-1]) for a in result.plan.actions}
    assert ("flag-orphan-sidecar", "IMG_404.JPG.supplemental-metadata.json") in kinds
    assert ("flag-unknown-file", "notes.txt") in kinds
    broken = items(tmp_path)["Takeout/Google Photos/Photos from 2019/broken.jpg"]
    assert "bad-sidecar" in broken.flags
    assert broken.sidecar_path is None


def test_an_extracted_tree_is_ingested_in_place(tmp_path: Path) -> None:
    standard_takeout(seed=0).write_tree(tmp_path / "tree")

    ingest(tmp_path / "tree", tmp_path, apply=True)

    row = items(tmp_path)["Takeout/Google Photos/Photos from 2019/IMG_1001.JPG"]
    assert row.original_path == "Takeout/Google Photos/Photos from 2019/IMG_1001.JPG"
    assert "match-exact" in row.flags


def test_a_stage_that_was_never_used_closes_cleanly(tmp_path: Path) -> None:
    with IngestStage(tmp_path / "nowhere", tmp_path / "work", zone=ZONE):
        pass


ROOT = "Takeout/Google Photos"


def test_an_item_in_three_albums_is_one_item_with_three_memberships(applied) -> None:  # type: ignore[no-untyped-def]
    _, tmp, _, _ = applied
    key = f"{ROOT}/Photos from 2019/IMG_1001.JPG"

    with Catalogue.open(tmp / "catalogue.sqlite") as catalogue:
        item = catalogue.get_item("google", key)
        assert item is not None
        assert item.id is not None
        assert catalogue.albums_of(item.id) == ["Best of", "Family", "Été 2019"]
        assert catalogue.copies_of(item.id) == [
            f"{ROOT}/Best of/IMG_1001.JPG",
            f"{ROOT}/Family/IMG_1001.JPG",
            f"{ROOT}/Été 2019/IMG_1001.JPG",
        ]
        assert catalogue.get_item("google", f"{ROOT}/Family/IMG_1001.JPG") is None


def test_albums_keep_their_title_description_and_sharing(applied) -> None:  # type: ignore[no-untyped-def]
    manifest, tmp, _, _ = applied

    with Catalogue.open(tmp / "catalogue.sqlite") as catalogue:
        albums = {a.title: a for a in catalogue.albums()}

    assert set(albums) == set(manifest.albums)
    summer = albums["Été 2019"]
    assert (summer.source_key, summer.description, summer.shared) == (
        f"{ROOT}/Été 2019",
        "Vacances 🌞",
        True,
    )
    assert albums["Best of"].shared is False


def test_items_that_live_only_in_an_album_still_get_a_membership(applied) -> None:  # type: ignore[no-untyped-def]
    _, tmp, _, _ = applied

    with Catalogue.open(tmp / "catalogue.sqlite") as catalogue:
        export = catalogue.get_item("google", f"{ROOT}/Exports/IMG_1003.jpg")
        assert export is not None
        assert export.id is not None
        assert catalogue.albums_of(export.id) == ["Exports"]


def test_favourites_captions_and_people_keywords_persist(applied) -> None:  # type: ignore[no-untyped-def]
    _, tmp, _, _ = applied
    stored = items(tmp)
    beach = stored[f"{ROOT}/Photos from 2019/IMG_1001.JPG"]
    tower = stored[f"{ROOT}/Photos from 2019/IMG_1003.HEIC"]

    assert beach.favorite is True
    assert beach.description == "Plage à Nice 🏖️"
    assert tower.description == "東京タワー"
    assert tower.favorite is False
    with Catalogue.open(tmp / "catalogue.sqlite") as catalogue:
        assert beach.id is not None
        assert catalogue.keywords(beach.id) == {"People/Alice Example", "People/Bob Example"}


def test_the_same_bytes_under_a_different_google_item_stay_separate(tmp_path: Path) -> None:
    b = TakeoutBuilder(seed=8)
    taken = datetime(2019, 7, 1, tzinfo=UTC)
    original = b.photo("Photos from 2019", "IMG_1.JPG", taken=taken)
    b.album("Holiday")
    b.photo(
        "Holiday", "IMG_9.JPG", taken=taken,
        image=b._sources[original.path].image,  # same pixels, same bytes, new upload
    )  # fmt: skip
    b.write_zips(tmp_path / "in")

    ingest(tmp_path / "in", tmp_path, apply=True)

    stored = items(tmp_path)
    assert len(stored) == 2
    assert "same-bytes-as-another-item" in stored[f"{ROOT}/Holiday/IMG_9.JPG"].flags


def test_an_album_with_broken_metadata_is_named_after_its_folder(tmp_path: Path) -> None:
    b = TakeoutBuilder(seed=9)
    b.photo("Road trip", "IMG_1.JPG", taken=datetime(2019, 7, 1, tzinfo=UTC))
    b.other_product("Google Photos/Road trip/metadata.json", b"{")
    b.write_zips(tmp_path / "in")

    ingest(tmp_path / "in", tmp_path, apply=True)

    with Catalogue.open(tmp_path / "catalogue.sqlite") as catalogue:
        assert [a.title for a in catalogue.albums()] == ["Road trip"]


def test_a_copy_in_a_plain_folder_is_recorded_without_an_album(tmp_path: Path) -> None:
    b = TakeoutBuilder(seed=10)
    photo = b.photo("Photos from 2019", "IMG_1.JPG", taken=datetime(2019, 7, 1, tzinfo=UTC))
    b.copy_to(photo, "Photos from 2020")
    b.write_zips(tmp_path / "in")

    ingest(tmp_path / "in", tmp_path, apply=True)

    with Catalogue.open(tmp_path / "catalogue.sqlite") as catalogue:
        (item,) = catalogue.items("google")
        assert item.id is not None
        assert catalogue.copies_of(item.id) == [f"{ROOT}/Photos from 2020/IMG_1.JPG"]
        assert catalogue.albums() == []


def test_live_photo_motion_clips_keep_their_content_identifier(applied) -> None:  # type: ignore[no-untyped-def]
    manifest, tmp, _, _ = applied
    stored = items(tmp)

    for item in manifest.with_quirk("live-photo-motion"):
        assert stored[item.path].content_id == item.content_id


def test_google_photos_get_perceptual_hashes(applied) -> None:  # type: ignore[no-untyped-def]
    _, tmp, _, _ = applied
    stored = items(tmp)

    beach = stored[f"{ROOT}/Photos from 2019/IMG_1001.JPG"]
    assert beach.phash is not None
    assert beach.dhash is not None
    assert beach.phash_fine is not None
    assert len(beach.phash_fine) == 64
    clip = stored[f"{ROOT}/Photos from 2019/IMG_1010.MOV"]
    assert clip.phash is None


def test_google_videos_get_a_signature_and_a_bitrate(applied) -> None:  # type: ignore[no-untyped-def]
    _, tmp, _, _ = applied
    stored = items(tmp)

    clip = stored[f"{ROOT}/Photos from 2019/VID_20190704_101010.mp4"]
    assert clip.video_signature is not None
    assert len(clip.video_signature.split(",")) == 8
    assert clip.bitrate is not None
    assert clip.bitrate > 0
    assert stored[f"{ROOT}/Photos from 2019/IMG_1001.JPG"].video_signature is None


def test_without_an_embedder_or_face_reader_nothing_of_either_is_stored(applied) -> None:  # type: ignore[no-untyped-def]
    _, tmp, _, _ = applied

    assert all(row.embedding is None and row.faces is None for row in items(tmp).values())


def test_photos_get_an_embedding_and_faces_from_the_stage_readers(tmp_path: Path) -> None:
    builder = TakeoutBuilder(seed=4)
    when = datetime(2021, 6, 1, 12, 0, tzinfo=UTC)
    builder.photo("Photos from 2021", "a.jpg", taken=when)
    builder.video("Photos from 2021", "b.mp4", taken=when)
    builder.write_zips(tmp_path / "takeout")
    faces = FixedFaces()

    with IngestStage(
        tmp_path / "takeout",
        tmp_path / "work",
        zone=ZONE,
        embedder=ThumbnailEmbedder(),
        faces=faces,
    ) as stage:
        run_stage(
            stage, catalogue_path=tmp_path / "catalogue.sqlite", plan_dir=tmp_path / "plans",
            apply=True,
        )  # fmt: skip

    stored = items(tmp_path)
    photo = stored["Takeout/Google Photos/Photos from 2021/a.jpg"]
    video = stored["Takeout/Google Photos/Photos from 2021/b.mp4"]
    assert photo.embedding is not None
    assert from_bytes(photo.embedding).shape == (256,)
    assert faces_from_json(photo.faces) == list(ONE_FACE)
    assert (video.embedding, video.faces) == (None, None)
    assert len(faces.seen) == 1, "videos are not read for faces"


def test_the_camera_is_recorded(applied) -> None:  # type: ignore[no-untyped-def]
    _, tmp, _, _ = applied
    stored = items(tmp)

    assert stored["Takeout/Google Photos/Photos from 2019/IMG_1001.JPG"].camera == "Apple iPhone 8"
    clip = stored["Takeout/Google Photos/Photos from 2019/VID_20190704_101010.mp4"]
    assert clip.camera is None
