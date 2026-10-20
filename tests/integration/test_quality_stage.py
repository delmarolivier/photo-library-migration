"""quality: measurements taken at ingest, flags with scores from the configured thresholds."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import ImageEnhance, ImageFilter
from typer.testing import CliRunner

from photomigrate.analyse.quality import QualityStage
from photomigrate.catalogue import Catalogue
from photomigrate.cli import app
from photomigrate.config import Config, load_config
from photomigrate.ingest.livephotos import LivePhotoStage
from photomigrate.ingest.stage import IngestStage
from photomigrate.runner import RunResult, run_stage
from photomigrate.testing.media import scene_frames, synthetic_photo
from photomigrate.testing.takeout import TakeoutBuilder

FOLDER = "Photos from 2021"
ROOT = f"Takeout/Google Photos/{FOLDER}"
WHEN = datetime(2021, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def catalogue_path(tmp_path: Path) -> Path:
    builder = TakeoutBuilder(seed=8)
    sharp = synthetic_photo(21, size=(800, 600))
    builder.photo(FOLDER, "sharp.jpg", taken=WHEN, image=sharp)
    builder.photo(FOLDER, "soft.jpg", taken=WHEN, image=sharp.filter(ImageFilter.GaussianBlur(4)))
    builder.photo(
        FOLDER, "dark.jpg", taken=WHEN, image=ImageEnhance.Brightness(sharp).enhance(0.25)
    )
    builder.video(FOLDER, "walk.mp4", taken=WHEN, frames=scene_frames(3, seconds=3.0))
    builder.video(FOLDER, "blip.mp4", taken=WHEN, seconds=0.8)
    builder.live_photo(FOLDER, "IMG_0001", taken=WHEN)
    builder.write_zips(tmp_path / "takeout")
    path = tmp_path / "catalogue.sqlite"
    common = {"catalogue_path": path, "plan_dir": tmp_path / "plans", "apply": True}
    with IngestStage(tmp_path / "takeout", tmp_path / "work", zone=ZoneInfo("UTC")) as stage:
        run_stage(stage, **common)  # type: ignore[arg-type]
    run_stage(LivePhotoStage(), **common)  # type: ignore[arg-type]
    return path


def flag(path: Path, tmp_path: Path, config: Config | None = None, apply: bool = True) -> RunResult:
    return run_stage(
        QualityStage((config or Config()).quality),
        catalogue_path=path,
        plan_dir=tmp_path / "plans",
        apply=apply,
    )


def flags(path: Path) -> dict[str, set[str]]:
    with Catalogue.open(path) as catalogue:
        found = catalogue.quality_flags()
        return {
            item.source_key.removeprefix(ROOT + "/"): set(found.get(item.id or 0, {}))
            for item in catalogue.items()
        }


def test_ingest_measures_photos_and_videos(catalogue_path: Path) -> None:
    with Catalogue.open(catalogue_path) as catalogue:
        measured = {
            i.source_key.removeprefix(ROOT + "/"): json.loads(i.quality) for i in catalogue.items()
        }

    assert {"sharpness", "brightness", "contrast", "megapixels"} <= set(measured["sharp.jpg"])
    assert measured["soft.jpg"]["sharpness"] < measured["sharp.jpg"]["sharpness"]
    assert set(measured["walk.mp4"]) == {"brightness", "motion"}


def test_each_item_gets_the_flags_for_its_problems(catalogue_path: Path, tmp_path: Path) -> None:
    result = flag(catalogue_path, tmp_path)

    found = flags(catalogue_path)
    assert found["sharp.jpg"] == set()
    assert found["soft.jpg"] == {"blurry"}
    assert "under-exposed" in found["dark.jpg"]
    assert found["walk.mp4"] == set()
    assert {"short-clip", "tiny-video"} <= found["blip.mp4"]
    assert "short-clip" not in found["IMG_0001.MOV"], (
        "a Live Photo's motion half is short by design"
    )
    assert result.applied == sum(1 for f in found.values() if f)


def test_flags_carry_scores_and_never_decide(catalogue_path: Path, tmp_path: Path) -> None:
    flag(catalogue_path, tmp_path)

    with Catalogue.open(catalogue_path) as catalogue:
        scores = [s for f in catalogue.quality_flags().values() for s in f.values()]
        assert all(0 < s <= 1 for s in scores)
        assert all(not catalogue.groups(kind) for kind in ("duplicate", "best-shot"))


def test_re_running_changes_nothing(catalogue_path: Path, tmp_path: Path) -> None:
    flag(catalogue_path, tmp_path)

    again = flag(catalogue_path, tmp_path)

    assert again.applied == 0
    assert again.plan.actions == ()


def test_a_dry_run_stores_no_flags(catalogue_path: Path, tmp_path: Path) -> None:
    result = flag(catalogue_path, tmp_path, apply=False)

    assert result.plan.actions
    assert all(not f for f in flags(catalogue_path).values())


def test_changed_thresholds_update_the_flags_and_back(catalogue_path: Path, tmp_path: Path) -> None:
    config_file = tmp_path / "strict.toml"
    config_file.write_text("[quality.photo]\nblurry_below_sharpness = 1e9\n", encoding="utf-8")
    flag(catalogue_path, tmp_path)

    flag(catalogue_path, tmp_path, load_config(config_file))
    assert "blurry" in flags(catalogue_path)["sharp.jpg"]

    flag(catalogue_path, tmp_path)
    assert flags(catalogue_path)["sharp.jpg"] == set()


def test_analyse_reads_thresholds_from_the_config_file(
    catalogue_path: Path, tmp_path: Path
) -> None:
    config_file = tmp_path / "photomigrate.toml"
    config_file.write_text("[quality.video]\nshort_below_seconds = 60\n", encoding="utf-8")
    args = ["--catalogue", str(catalogue_path), "--plan-dir", str(tmp_path / "plans")]

    result = CliRunner().invoke(app, [*args, "--config", str(config_file), "analyse", "--apply"])

    assert result.exit_code == 0, result.output
    assert "short-clip" in flags(catalogue_path)["walk.mp4"]


def test_a_bad_config_file_stops_before_anything_runs(catalogue_path: Path, tmp_path: Path) -> None:
    config_file = tmp_path / "photomigrate.toml"
    config_file.write_text("[quality.photo]\nblury = 3\n", encoding="utf-8")
    args = ["--catalogue", str(catalogue_path), "--config", str(config_file)]

    result = CliRunner().invoke(app, [*args, "analyse", "--apply"])

    assert result.exit_code == 2
    # Rich may colour the error panel and wrap it inside its borders
    text = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", result.output).replace("│", " ").split())
    assert "unknown setting 'quality.photo.blury'" in text
    assert all(not f for f in flags(catalogue_path).values())
