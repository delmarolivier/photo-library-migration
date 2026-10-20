"""`photomigrate index`: a read-only catalogue of the iCloud Photos library (CLA-79)."""

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.cli import app
from photomigrate.faces import VisionFaces, faces_from_json
from photomigrate.index.icloud import IndexStage
from photomigrate.runner import RunResult, run_stage
from photomigrate.testing.embedder import ThumbnailEmbedder

pytestmark = pytest.mark.macos


def snapshot(library: Path) -> dict[str, tuple[str, int]]:
    return {
        str(p.relative_to(library)): (
            hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mtime_ns,
        )
        for p in sorted(library.rglob("*"))
        if p.is_file()
    }


def index(library: Path, tmp: Path, *, apply: bool = True) -> tuple[RunResult, IndexStage]:
    with IndexStage(library) as stage:
        result = run_stage(
            stage, catalogue_path=tmp / "catalogue.sqlite", plan_dir=tmp / "plans", apply=apply
        )
    return result, stage


def icloud_items(tmp: Path) -> dict[str, CatalogueItem]:
    with Catalogue.open(tmp / "catalogue.sqlite") as catalogue:
        return {item.title or "": item for item in catalogue.items("icloud")}


def test_indexing_never_writes_to_the_library(test_library: Path, tmp_path: Path) -> None:
    before = snapshot(test_library)

    dry, _ = index(test_library, tmp_path / "dry", apply=False)
    result, stage = index(test_library, tmp_path / "wet")

    assert snapshot(test_library) == before, "the library must be byte- and mtime-identical"
    assert stage.read_only.violations == []
    assert result.applied == len(dry.plan.actions) == 13


def test_a_write_attempt_during_indexing_is_refused(test_library: Path, tmp_path: Path) -> None:
    from photomigrate.safety import ForbiddenPathError

    with IndexStage(test_library), pytest.raises(ForbiddenPathError, match="read-only"):
        (test_library / "database" / "Photos.sqlite").write_bytes(b"oops")


def test_each_item_records_identity_hashes_dates_place_and_size(
    test_library: Path, tmp_path: Path
) -> None:
    index(test_library, tmp_path)
    stored = icloud_items(tmp_path)

    item = stored["IMG_4547.jpg"]
    assert item.source == "icloud"
    assert len(item.source_key) == 36  # the Photos uuid
    assert item.original_path is not None
    assert item.original_path.startswith("originals/")
    assert (
        item.sha256 == hashlib.sha256((test_library / item.original_path).read_bytes()).hexdigest()
    )
    assert item.phash is not None
    assert item.dhash is not None
    assert item.phash_fine is not None
    assert json.loads(item.quality)["megapixels"] == pytest.approx(7.585, abs=0.001)
    assert (item.width, item.height) == (2754, 2754)
    assert item.latitude == pytest.approx(-34.91889167)
    assert item.timezone == "Australia/Adelaide"
    assert item.kind == "photo"
    with Catalogue.open(tmp_path / "catalogue.sqlite") as catalogue:
        assert item.id is not None
        when = catalogue.date_candidates(item.id)["icloud"]
    assert when.isoformat() == "2017-06-20T17:18:56+09:30"


def test_edits_favourites_albums_keywords_captions_and_scores(
    test_library: Path, tmp_path: Path
) -> None:
    index(test_library, tmp_path)

    with Catalogue.open(tmp_path / "catalogue.sqlite") as catalogue:
        items = catalogue.items("icloud")
        assert sum("edited" in i.flags for i in items) == 4
        assert sum(i.favorite for i in items) == 1
        assert sum(bool(catalogue.keywords(i.id or 0)) for i in items) == 7
        assert {a.source for a in catalogue.albums()} == {"icloud"}
        assert len(catalogue.albums()) >= 1
        assert any(catalogue.albums_of(i.id or 0) for i in items)
        assert any(i.description for i in items)
        scores = [json.loads(i.scores) for i in items]
    assert all("overall" in s for s in scores)


def test_live_photos_and_videos(live_library: Path, tmp_path: Path) -> None:
    index(live_library, tmp_path)

    stored = icloud_items(tmp_path)
    live = stored["IMG_4062.HEIC"]
    assert "live-photo" in live.flags
    assert live.content_id
    movie = next(i for i in stored.values() if i.kind == "video")
    assert movie.duration is not None
    assert movie.duration > 0
    assert movie.video_signature is not None
    assert movie.bitrate is not None
    assert set(json.loads(movie.quality)) == {"brightness", "motion"}


def test_missing_originals_are_reported_not_skipped(test_library: Path, tmp_path: Path) -> None:
    gone = next(p for p in sorted((test_library / "originals").rglob("*")) if p.is_file())
    gone.unlink()  # a cloud-only original: the library still lists it

    result, _ = index(test_library, tmp_path)

    missing = [a for a in result.plan.actions if a.kind == "index-missing"]
    assert len(missing) == 1
    (item,) = [i for i in icloud_items(tmp_path).values() if "original-missing" in i.flags]
    assert item.sha256 is None
    assert item.phash is None
    assert len(icloud_items(tmp_path)) == 13


def test_re_indexing_changes_nothing(test_library: Path, tmp_path: Path) -> None:
    index(test_library, tmp_path)
    before = (tmp_path / "catalogue.sqlite").read_bytes()

    again, _ = index(test_library, tmp_path)

    assert again.applied == 0
    assert (tmp_path / "catalogue.sqlite").read_bytes() == before


def test_the_cli_is_a_dry_run_by_default_and_lists_missing_originals(
    test_library: Path, tmp_path: Path
) -> None:
    gone = next(p for p in sorted((test_library / "originals").rglob("*")) if p.is_file())
    gone.unlink()
    args = ["--catalogue", str(tmp_path / "c.sqlite"), "--plan-dir", str(tmp_path / "plans")]

    dry = CliRunner().invoke(app, [*args, "index", str(test_library)])

    assert dry.exit_code == 0, dry.output
    assert "Dry run" in dry.output
    assert "1 original is not on this Mac" in dry.output
    assert not (tmp_path / "c.sqlite").exists()
    wet = CliRunner().invoke(app, [*args, "index", str(test_library), "--apply"])
    assert wet.exit_code == 0, wet.output
    assert "Applied 13 actions" in wet.output


def test_the_cli_rejects_something_that_is_not_a_photos_library(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["index", str(tmp_path)])

    assert result.exit_code == 1
    assert "not a Photos library" in result.output


def test_photos_get_an_embedding_and_their_faces(test_library: Path, tmp_path: Path) -> None:
    with IndexStage(test_library, embedder=ThumbnailEmbedder(), faces=VisionFaces()) as stage:
        run_stage(
            stage, catalogue_path=tmp_path / "catalogue.sqlite", plan_dir=tmp_path / "plans",
            apply=True,
        )  # fmt: skip

    stored = icloud_items(tmp_path)
    photos = [i for i in stored.values() if i.kind == "photo" and i.sha256]
    assert all(i.embedding is not None for i in photos)
    assert all(faces_from_json(i.faces) is not None for i in photos)
    by_name = {name.lower(): item for name, item in stored.items()}
    wedding = next(item for name, item in by_name.items() if "wedding" in name)
    tulips = next(item for name, item in by_name.items() if "tulips" in name)
    (face,) = faces_from_json(wedding.faces) or []
    assert face.eyes_open is True
    assert faces_from_json(tulips.faces) == []


def test_without_an_embedder_or_face_reader_neither_is_stored(
    test_library: Path, tmp_path: Path
) -> None:
    index(test_library, tmp_path)

    assert all(i.embedding is None and i.faces is None for i in icloud_items(tmp_path).values())


def test_the_cli_embeds_photos_and_reads_their_faces(test_library: Path, tmp_path: Path) -> None:
    args = ["--catalogue", str(tmp_path / "catalogue.sqlite"), "--plan-dir", str(tmp_path / "p")]

    result = CliRunner().invoke(app, [*args, "index", str(test_library), "--apply"])

    assert result.exit_code == 0, result.output
    photos = [i for i in icloud_items(tmp_path).values() if i.kind == "photo" and i.sha256]
    assert all(i.embedding is not None and i.faces is not None for i in photos)
