"""The best-shot stage over a real catalogue: bursts grouped, top two kept, the rest proposed."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import ImageEnhance, ImageFilter
from typer.testing import CliRunner

from photomigrate.analyse.best_shot import BestShotStage
from photomigrate.analyse.dedupe_photo import DedupeStage
from photomigrate.catalogue import Catalogue
from photomigrate.cli import app
from photomigrate.config import BestShotConfig, BestShotWeights
from photomigrate.faces import Face, faces_to_json
from photomigrate.ingest.livephotos import LivePhotoStage
from photomigrate.ingest.stage import IngestStage
from photomigrate.runner import RunResult, run_stage
from photomigrate.testing.embedder import ThumbnailEmbedder
from photomigrate.testing.media import synthetic_photo
from photomigrate.testing.takeout import TakeoutBuilder

FOLDER = "Photos from 2021"
ROOT = f"Takeout/Google Photos/{FOLDER}"
T = datetime(2021, 6, 1, 12, 0, tzinfo=UTC)
SHARP = {"IMG_2001.JPG", "IMG_2002.JPG", "IMG_2005.JPG"}
BURST = {*SHARP, "IMG_2003.JPG", "IMG_2004.JPG", "IMG_2006.JPG"}


def at(seconds: float) -> datetime:
    return T + timedelta(seconds=seconds)


@pytest.fixture
def catalogue_path(tmp_path: Path) -> Path:
    b = TakeoutBuilder(seed=11)
    first = b.photo(FOLDER, "IMG_2001.JPG", taken=at(0), image=synthetic_photo(30))
    b.photo(FOLDER, "IMG_2002.JPG", taken=at(1), image=synthetic_photo(30, jitter=2))
    blurred = synthetic_photo(30, jitter=3).filter(ImageFilter.GaussianBlur(3))
    b.photo(FOLDER, "IMG_2003.JPG", taken=at(2), image=blurred)
    dark = ImageEnhance.Brightness(synthetic_photo(30, jitter=4)).enhance(0.35)
    b.photo(FOLDER, "IMG_2004.JPG", taken=at(3), image=dark)
    b.photo(FOLDER, "IMG_2005.JPG", taken=at(4), image=synthetic_photo(30, jitter=5))
    # no EXIF: the sidecar's date (the planned one) places it in the burst
    soft = synthetic_photo(30, jitter=8).filter(ImageFilter.GaussianBlur(2))
    b.photo(FOLDER, "IMG_2006.JPG", taken=at(5), exif_taken=None, image=soft)
    b.near_duplicate(first, FOLDER, "IMG_2001_small.jpg", kind="resized")
    b.photo(FOLDER, "IMG_4001.JPG", taken=at(6), image=synthetic_photo(50))
    # the same view ten minutes later: another moment
    b.photo(FOLDER, "IMG_2101.JPG", taken=at(600), image=synthetic_photo(30, jitter=1))
    b.photo(FOLDER, "IMG_2102.JPG", taken=at(601), image=synthetic_photo(30, jitter=2))
    # unrelated shots minutes apart
    b.photo(FOLDER, "IMG_3001.JPG", taken=at(900), image=synthetic_photo(41))
    b.photo(FOLDER, "IMG_3002.JPG", taken=at(1140), image=synthetic_photo(42))
    # a Live Photo and a second shot of it
    b.live_photo(FOLDER, "IMG_5001", taken=at(1200), image=synthetic_photo(60, (320, 240)))
    b.photo(FOLDER, "IMG_5002.JPG", taken=at(1201), image=synthetic_photo(60, (320, 240), jitter=2))
    # no date at all: left out
    b.photo(
        FOLDER, "IMG_6001.JPG", taken=at(7), exif_taken=None, sidecar=None,
        image=synthetic_photo(30, jitter=6),
    )  # fmt: skip
    b.write_zips(tmp_path / "takeout")
    path = tmp_path / "catalogue.sqlite"
    common = {"catalogue_path": path, "plan_dir": tmp_path / "plans", "apply": True}
    with IngestStage(
        tmp_path / "takeout", tmp_path / "work", zone=ZoneInfo("UTC"), embedder=ThumbnailEmbedder()
    ) as stage:
        run_stage(stage, **common)  # type: ignore[arg-type]
    run_stage(LivePhotoStage(), **common)  # type: ignore[arg-type]
    run_stage(DedupeStage(), **common)  # type: ignore[arg-type]
    return path


def best_shot(
    path: Path, tmp_path: Path, config: BestShotConfig | None = None, *, apply: bool = True
) -> RunResult:
    return run_stage(
        BestShotStage(config or BestShotConfig()),
        catalogue_path=path,
        plan_dir=tmp_path / "plans",
        apply=apply,
    )


def groups(path: Path) -> list[dict[str, str | None]]:
    """Each best-shot group as {file name: role}."""
    with Catalogue.open(path) as catalogue:
        return [
            {
                catalogue.item(item_id).source_key.removeprefix(ROOT + "/"): role
                for item_id, role, _ in catalogue.group_members(group.id)
            }
            for group in catalogue.groups("best-shot")
        ]


def group_with(path: Path, name: str) -> dict[str, str | None]:
    return next(g for g in groups(path) if name in g)


def test_a_burst_is_one_group_that_keeps_its_two_best_shots(
    catalogue_path: Path, tmp_path: Path
) -> None:
    best_shot(catalogue_path, tmp_path)

    burst = group_with(catalogue_path, "IMG_2001.JPG")
    assert set(burst) == BURST
    kept = {name for name, role in burst.items() if role == "keep"}
    assert len(kept) == 2
    assert kept <= SHARP
    assert {name for name, role in burst.items() if role == "to-delete"} == BURST - kept


def test_other_moments_and_unrelated_shots_stay_out_of_the_burst(
    catalogue_path: Path, tmp_path: Path
) -> None:
    best_shot(catalogue_path, tmp_path)

    found = groups(catalogue_path)
    assert sorted(sorted(g) for g in found) == [
        sorted(BURST),
        ["IMG_2101.JPG", "IMG_2102.JPG"],
        ["IMG_5001.HEIC", "IMG_5001.MOV", "IMG_5002.JPG"],
    ]
    grouped = {name for g in found for name in g}
    assert not grouped & {"IMG_3001.JPG", "IMG_3002.JPG", "IMG_4001.JPG", "IMG_6001.JPG"}
    assert "IMG_2001_small.jpg" not in grouped, "a proposed duplicate is dedupe's business"


def test_a_pair_keeps_both_and_a_live_photo_keeps_its_motion_clip(
    catalogue_path: Path, tmp_path: Path
) -> None:
    best_shot(catalogue_path, tmp_path)

    assert group_with(catalogue_path, "IMG_2101.JPG") == {
        "IMG_2101.JPG": "keep",
        "IMG_2102.JPG": "keep",
    }
    live = group_with(catalogue_path, "IMG_5001.HEIC")
    assert live == {"IMG_5001.HEIC": "keep", "IMG_5001.MOV": "motion", "IMG_5002.JPG": "keep"}


def test_each_group_records_the_scores_and_what_counted(
    catalogue_path: Path, tmp_path: Path
) -> None:
    result = best_shot(catalogue_path, tmp_path)

    assert result.plan.summary() == {"propose-best-shot": 3}
    with Catalogue.open(catalogue_path) as catalogue:
        details = [g.details for g in catalogue.groups("best-shot")]
    burst = next(d for d in details if len(d["scores"]) == len(BURST))
    assert burst["used"] == ["sharpness", "exposure"], "no Vision in this catalogue"
    assert len(burst["keep"]) == 2
    ranked = sorted(burst["scores"].items(), key=lambda kv: -kv[1])
    assert [name for name, _ in ranked[:2]] == burst["keep"]
    assert all(set(parts) == {"sharpness", "exposure"} for parts in burst["parts"].values())


def test_faces_and_apples_score_count_when_every_shot_has_them(
    catalogue_path: Path, tmp_path: Path
) -> None:
    pair = [f"{ROOT}/IMG_2101.JPG", f"{ROOT}/IMG_2102.JPG"]
    with Catalogue.open(catalogue_path) as catalogue:
        for key, eyes in zip(pair, (False, True), strict=True):
            item = catalogue.get_item("google", key)
            assert item is not None
            face = Face((0.4, 0.4, 0.2, 0.2), 0.7, eyes_open=eyes)
            catalogue.upsert_item(
                item.replace(faces=faces_to_json([face]), scores='{"overall": 0.8}')
            )

    best_shot(catalogue_path, tmp_path)

    with Catalogue.open(catalogue_path) as catalogue:
        details = next(
            g.details for g in catalogue.groups("best-shot") if len(g.details["scores"]) == 2
        )
    assert details["used"] == ["sharpness", "exposure", "faces", "eyes_open", "aesthetic"]
    assert details["keep"][0] == f"google:{pair[1]}", "eyes open beats a blink"


def test_re_running_changes_nothing(catalogue_path: Path, tmp_path: Path) -> None:
    best_shot(catalogue_path, tmp_path)

    again = best_shot(catalogue_path, tmp_path)

    assert again.applied == 0
    assert again.plan.actions == ()


def test_a_dry_run_stores_nothing(catalogue_path: Path, tmp_path: Path) -> None:
    result = best_shot(catalogue_path, tmp_path, apply=False)

    assert len(result.plan.actions) == 3
    assert groups(catalogue_path) == []


def test_groups_that_no_longer_hold_are_retired_and_come_back(
    catalogue_path: Path, tmp_path: Path
) -> None:
    best_shot(catalogue_path, tmp_path)

    strict = best_shot(catalogue_path, tmp_path, BestShotConfig(window_seconds=0.5))

    assert strict.plan.summary() == {"retire-best-shot": 3}
    assert groups(catalogue_path) == []
    best_shot(catalogue_path, tmp_path)
    assert len(groups(catalogue_path)) == 3


def test_new_weights_rerank_a_group(catalogue_path: Path, tmp_path: Path) -> None:
    best_shot(catalogue_path, tmp_path)
    exposure_only = BestShotConfig(weights=BestShotWeights(sharpness=0, aesthetic=0))

    result = best_shot(catalogue_path, tmp_path, exposure_only)

    assert set(result.plan.summary()) == {"propose-best-shot"}
    with Catalogue.open(catalogue_path) as catalogue:
        assert all(g.details["used"] == ["exposure"] for g in catalogue.groups("best-shot"))


def test_proposals_never_decide(catalogue_path: Path, tmp_path: Path) -> None:
    best_shot(catalogue_path, tmp_path)

    with sqlite3.connect(catalogue_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone() == (0,)


def test_analyse_runs_best_shot_with_the_config_file(catalogue_path: Path, tmp_path: Path) -> None:
    args = ["--catalogue", str(catalogue_path), "--plan-dir", str(tmp_path / "plans")]
    runner = CliRunner()

    result = runner.invoke(app, [*args, "analyse", "--apply"])

    assert result.exit_code == 0, result.output
    assert "best-shot" in result.output
    assert len(groups(catalogue_path)) == 3
    strict = tmp_path / "strict.toml"
    strict.write_text("[best_shot]\nmin_similarity = 1.0\n", encoding="utf-8")
    again = runner.invoke(app, [*args, "--config", str(strict), "analyse", "--apply"])
    assert again.exit_code == 0, again.output
    assert groups(catalogue_path) == []


def test_a_proposal_for_items_that_are_gone_is_an_error(tmp_path: Path) -> None:
    from photomigrate.runner import Action

    action = Action(
        "propose-best-shot",
        "best-x",
        {
            "used": [],
            "keep": [],
            "scores": {},
            "parts": {},
            "members": [["icloud", "GONE", "keep"]],
        },
    )
    with Catalogue.snapshot(tmp_path / "none") as catalogue, pytest.raises(KeyError, match="GONE"):
        BestShotStage(BestShotConfig()).apply(action, catalogue)
