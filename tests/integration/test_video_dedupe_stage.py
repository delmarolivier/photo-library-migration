"""dedupe-videos over the catalogue, alongside dedupe-photos (CLA-81)."""

import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from photomigrate.analyse.dedupe_photo import DedupeStage
from photomigrate.analyse.dedupe_video import VideoDedupeStage
from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.cli import app
from photomigrate.hashing import bitrate, video_signature
from photomigrate.ingest.livephotos import LivePhotoStage
from photomigrate.ingest.stage import IngestStage
from photomigrate.runner import run_stage
from photomigrate.testing.media import reencode_video
from photomigrate.testing.takeout import TakeoutManifest

ROOT = "Takeout/Google Photos/Photos from 2019"
CLIP = f"{ROOT}/VID_20190704_101010.mp4"


@pytest.fixture
def catalogue_path(standard_takeout_zips: TakeoutManifest, tmp_path: Path) -> Path:
    path = tmp_path / "catalogue.sqlite"
    common = {"catalogue_path": path, "plan_dir": tmp_path / "plans", "apply": True}
    with IngestStage(
        standard_takeout_zips.zips[0].parent, tmp_path / "work", zone=ZoneInfo("Europe/Paris")
    ) as stage:
        run_stage(stage, **common)  # type: ignore[arg-type]
    run_stage(LivePhotoStage(), **common)  # type: ignore[arg-type]
    return path


def member_bytes(manifest: TakeoutManifest, path: str) -> bytes:
    with zipfile.ZipFile(manifest.zips[manifest.parts[path] - 1]) as bundle:
        return bundle.read(path)


def add_icloud_copy(
    catalogue: Catalogue, tmp_path: Path, data: bytes, uuid: str, size: tuple[int, int]
) -> None:
    path = tmp_path / f"{uuid}.mov"
    path.write_bytes(data)
    signature = video_signature(path, 2.0)
    assert signature is not None
    catalogue.upsert_item(
        CatalogueItem(
            source="icloud", source_key=uuid, kind="video", duration=2.0, width=size[0],
            height=size[1], video_signature=",".join(signature), bitrate=bitrate(len(data), 2.0),
            sha256="0" * 64,
        )
    )  # fmt: skip


def video_groups(path: Path) -> list[list[tuple[str, str, str | None]]]:
    with Catalogue.open(path) as catalogue:
        return [
            [
                (catalogue.item(i).source, catalogue.item(i).source_key, role)
                for i, role, _ in catalogue.group_members(group.id)
            ]
            for group in catalogue.groups("duplicate")
            if group.key.startswith("vdup-")
        ]


def test_an_icloud_reencode_of_a_google_clip_is_proposed(
    catalogue_path: Path, standard_takeout_zips: TakeoutManifest, tmp_path: Path
) -> None:
    with Catalogue.open(catalogue_path) as catalogue:
        original = member_bytes(standard_takeout_zips, CLIP)
        add_icloud_copy(
            catalogue,
            tmp_path,
            reencode_video(original, "mov", scale=(32, 24), crf=35),
            "UUID-CLIP",
            (32, 24),
        )

    run_stage(
        VideoDedupeStage(), catalogue_path=catalogue_path, plan_dir=tmp_path / "plans", apply=True
    )

    assert video_groups(catalogue_path) == [
        [("google", CLIP, "keeper"), ("icloud", "UUID-CLIP", "duplicate")]
    ]


def test_live_photo_motion_clips_and_distinct_videos_are_left_alone(
    catalogue_path: Path, standard_takeout_zips: TakeoutManifest, tmp_path: Path
) -> None:
    with Catalogue.open(catalogue_path) as catalogue:
        motion = member_bytes(standard_takeout_zips, f"{ROOT}/IMG_2001.MOV")
        add_icloud_copy(catalogue, tmp_path, motion, "UUID-MOTION", (64, 48))

    result = run_stage(
        VideoDedupeStage(), catalogue_path=catalogue_path, plan_dir=tmp_path / "plans", apply=True
    )

    assert result.plan.actions == ()
    assert video_groups(catalogue_path) == []


def test_photo_and_video_stages_leave_each_others_groups_alone(
    catalogue_path: Path, standard_takeout_zips: TakeoutManifest, tmp_path: Path
) -> None:
    with Catalogue.open(catalogue_path) as catalogue:
        original = member_bytes(standard_takeout_zips, CLIP)
        add_icloud_copy(
            catalogue, tmp_path, reencode_video(original, "mov", crf=30), "UUID-CLIP", (64, 48)
        )
    common = {"catalogue_path": catalogue_path, "plan_dir": tmp_path / "plans", "apply": True}

    run_stage(VideoDedupeStage(), **common)  # type: ignore[arg-type]
    photos = run_stage(DedupeStage(), **common)  # type: ignore[arg-type]
    again = run_stage(VideoDedupeStage(), **common)  # type: ignore[arg-type]

    assert all(a.kind == "propose-duplicates" for a in photos.plan.actions)
    assert again.applied == 0
    assert len(video_groups(catalogue_path)) == 1


def test_analyse_runs_photo_and_video_dedupe(catalogue_path: Path, tmp_path: Path) -> None:
    args = ["--catalogue", str(catalogue_path), "--plan-dir", str(tmp_path / "plans"), "analyse"]

    result = CliRunner().invoke(app, [*args, "--apply", "--signature-max", "8"])

    assert result.exit_code == 0, result.output
    assert "dedupe-photos" in result.output
    assert "dedupe-videos" in result.output
