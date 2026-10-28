"""The highlights stage over a real catalogue: each year keeps its share, the rest proposed."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from photomigrate.analyse.highlights import HighlightsStage
from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.cli import app
from photomigrate.config import BestShotWeights, HighlightsConfig
from photomigrate.embedding import to_bytes
from photomigrate.quality import PhotoMetrics
from photomigrate.runner import RunResult, run_stage
from photomigrate.testing.embedder import ThumbnailEmbedder
from photomigrate.testing.media import synthetic_image

EMBEDDER = ThumbnailEmbedder()
T2014 = datetime(2014, 5, 1, 10, 0, tzinfo=UTC)
T2015 = datetime(2015, 8, 1, 10, 0, tzinfo=UTC)


def quality(sharpness: float) -> str:
    return json.dumps(
        PhotoMetrics(
            sharpness=sharpness, brightness=128.0, dark_fraction=0.0, bright_fraction=0.0,
            contrast=50.0, megapixels=12.0, short_side=3024,
        ).to_dict()
    )  # fmt: skip


def add(
    catalogue: Catalogue,
    name: str,
    taken: datetime,
    *,
    seed: int,
    sharpness: float = 200.0,
    kind: str = "photo",
    source: str = "google",
    **fields: object,
) -> int:
    item_id = catalogue.upsert_item(
        CatalogueItem(
            source=source,  # type: ignore[arg-type]
            source_key=name,
            kind=kind,  # type: ignore[arg-type]
            width=4000,
            height=3000,
            quality=quality(sharpness) if kind == "photo" else "{}",
            embedding=to_bytes(EMBEDDER.embed(synthetic_image(seed, (64, 48)))),
            **fields,  # type: ignore[arg-type]
        )
    )
    catalogue.set_date_candidate(item_id, "exif", taken)
    return item_id


@pytest.fixture
def path(tmp_path: Path) -> Path:
    path = tmp_path / "catalogue.sqlite"
    with Catalogue.open(path) as catalogue:
        # 2014: ten shots of one afternoon, two of them sharp; one favourite, one in an album
        for n in range(10):
            add(catalogue, f"2014/IMG_{n:04d}.JPG", T2014 + timedelta(minutes=n), seed=n,
                sharpness=400.0 if n in (3, 6) else 100.0)  # fmt: skip
        add(catalogue, "2014/starred.JPG", T2014 + timedelta(minutes=20), seed=40, favorite=True)
        album = catalogue.upsert_album("google", "Takeout/Google Photos/Trip", "Trip")
        catalogue.add_album_member(
            album, add(catalogue, "2014/trip.JPG", T2014 + timedelta(minutes=21), seed=41)
        )
        # a video, a duplicate, a best-shot loser and clutter: none of them count
        add(catalogue, "2014/clip.MP4", T2014 + timedelta(minutes=30), seed=50, kind="video")
        keeper = add(catalogue, "2014/dup-keeper.JPG", T2014 + timedelta(hours=1), seed=51)
        copy = add(catalogue, "2014/dup-copy.JPG", T2014 + timedelta(hours=1), seed=51)
        group = catalogue.upsert_group("duplicate", "dup-a", {"keeper": "x", "method": "exact"})
        catalogue.set_group_members(group, [(keeper, "keeper", 0), (copy, "duplicate", 1)])
        loser = add(catalogue, "2014/burst-loser.JPG", T2014 + timedelta(hours=1, seconds=5),
                    seed=52)  # fmt: skip
        group = catalogue.upsert_group("best-shot", "best-a", {"keep": [], "scores": {}})
        catalogue.set_group_members(group, [(keeper, "keep", 0), (loser, "to-delete", 1)])
        junk = add(catalogue, "2014/screenshot.PNG", T2014 + timedelta(hours=2), seed=53)
        catalogue.set_clutter(junk, "screenshot", 0.9, {})
        # 2015: five shots on two days far apart
        for n in range(3):
            add(catalogue, f"2015/A_{n}.JPG", T2015 + timedelta(minutes=n), seed=60 + n)
        for n in range(2):
            add(catalogue, f"2015/B_{n}.JPG", T2015 + timedelta(days=9, minutes=n), seed=70 + n)
    return path


def highlights(path: Path, tmp_path: Path, *, apply: bool = True) -> RunResult:
    return run_stage(
        HighlightsStage(HighlightsConfig(), BestShotWeights(), appeal=None),
        catalogue_path=path,
        plan_dir=tmp_path / "plans",
        apply=apply,
    )


def roles(path: Path) -> dict[str, str | None]:
    """Every item in a highlights group, by name, with its role."""
    with Catalogue.open(path) as catalogue:
        return {
            catalogue.item(item_id).source_key: role
            for group in catalogue.groups("highlights")
            for item_id, role, _ in catalogue.group_members(group.id)
        }


def test_each_year_keeps_its_share_with_favourites_and_albums_first(
    path: Path, tmp_path: Path
) -> None:
    highlights(path, tmp_path)

    found = roles(path)
    kept = {name for name, role in found.items() if role == "keep" and name.startswith("2014")}
    # 2014 counts 13 photos (10, the favourite, the album photo, the duplicate keeper): 3 kept
    assert {"2014/starred.JPG", "2014/trip.JPG"} <= kept
    assert len(kept) == 3
    assert kept & {"2014/IMG_0003.JPG", "2014/IMG_0006.JPG"}


def test_videos_duplicates_best_shot_losers_and_clutter_are_left_out(
    path: Path, tmp_path: Path
) -> None:
    highlights(path, tmp_path)

    found = roles(path)
    for name in ("clip.MP4", "dup-copy.JPG", "burst-loser.JPG", "screenshot.PNG"):
        assert f"2014/{name}" not in found


def test_every_event_of_a_year_gets_a_group_of_its_own(path: Path, tmp_path: Path) -> None:
    highlights(path, tmp_path)

    with Catalogue.open(path) as catalogue:
        years = sorted(
            (g.details["year"], len(catalogue.group_members(g.id)))
            for g in catalogue.groups("highlights")
        )
    # 2015: 5 photos, a share of 1, which goes to the larger day; the other day keeps
    # nothing, so it is still asked about
    assert [year for year, _ in years].count(2015) == 2
    found = roles(path)
    assert sum(role == "keep" for name, role in found.items() if name.startswith("2015")) == 1


def test_details_hold_the_year_the_picks_and_why_some_are_protected(
    path: Path, tmp_path: Path
) -> None:
    highlights(path, tmp_path)

    with Catalogue.open(path) as catalogue:
        group = next(
            g
            for g in catalogue.groups("highlights")
            if "google:2014/starred.JPG" in g.details["keep"]
        )
    assert group.details["year"] == 2014
    assert group.details["protected"] == {
        "google:2014/starred.JPG": "favourite",
        "google:2014/trip.JPG": "album",
    }
    assert set(group.details["scores"]) >= set(group.details["keep"])


def test_a_dry_run_changes_nothing_and_a_rerun_is_a_no_op(path: Path, tmp_path: Path) -> None:
    planned = highlights(path, tmp_path, apply=False)
    assert roles(path) == {}

    highlights(path, tmp_path)
    again = highlights(path, tmp_path)

    assert planned.plan.actions
    assert again.plan.actions == ()


def test_groups_that_no_longer_fit_are_retired(path: Path, tmp_path: Path) -> None:
    highlights(path, tmp_path)
    with Catalogue.open(path) as catalogue:
        for item in catalogue.items():
            if item.source_key.startswith("2015/B_"):
                catalogue.upsert_item(item.replace(favorite=True))

    highlights(path, tmp_path)

    assert not any(name.startswith("2015/B_") for name in roles(path))


def test_analyse_proposes_the_highlights(path: Path, tmp_path: Path) -> None:
    args = ["--catalogue", str(path), "--plan-dir", str(tmp_path / "plans")]

    result = CliRunner().invoke(app, [*args, "analyse", "--apply"])

    assert result.exit_code == 0, result.output
    assert "highlights" in result.output
    assert any(role == "keep" for role in roles(path).values())


def test_an_action_for_an_item_that_is_gone_is_an_error(tmp_path: Path) -> None:
    from photomigrate.runner import Action

    action = Action(
        "propose-highlights", "hl-x", {"year": 2014, "members": [["google", "GONE", "keep"]]}
    )
    stage = HighlightsStage(HighlightsConfig(), BestShotWeights(), appeal=None)
    with Catalogue.snapshot(tmp_path / "none") as catalogue, pytest.raises(KeyError, match="GONE"):
        stage.apply(action, catalogue)
