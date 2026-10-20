"""The location stage over a catalogue: proposals only, written after the review approves."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from photomigrate.analyse.locations import LocationStage, approved_location
from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.cli import app
from photomigrate.config import LocationConfig
from photomigrate.runner import RunResult, run_stage
from photomigrate.testing.timeline import TimelineBuilder
from photomigrate.timeline import parse_timeline

ROOT = "Takeout/Google Photos/Photos from 2025"
PARIS = timezone(timedelta(hours=2))
CAMERA = "Apple iPhone 12"


def add(
    catalogue: Catalogue,
    name: str,
    taken: datetime,
    at: tuple[float, float] | None = None,
    *,
    source: str = "google",
    **changes: object,
) -> int:
    key = f"{ROOT}/{name}" if source == "google" else name
    latitude, longitude = at if at else (None, None)
    item = CatalogueItem(
        source=source, source_key=key, kind="photo", title=name, camera=CAMERA,  # type: ignore[arg-type]
        latitude=latitude, longitude=longitude,
    ).replace(**changes)  # fmt: skip
    item_id = catalogue.upsert_item(item)
    catalogue.set_date_candidate(item_id, "exif" if source == "google" else "icloud", taken)
    return item_id


@pytest.fixture
def catalogue_path(tmp_path: Path) -> Path:
    path = tmp_path / "catalogue.sqlite"
    noon = datetime(2025, 6, 1, 12, 0)
    with Catalogue.open(path) as catalogue:
        add(catalogue, "IMG_0001.JPG", noon, (48.0, 2.0))
        add(catalogue, "IMG_0002.JPG", noon + timedelta(minutes=10))
        add(catalogue, "IMG_0003.JPG", noon + timedelta(minutes=20), (48.2, 2.4))
        add(catalogue, "IMG_0004.JPG", noon + timedelta(hours=2))
        shot = add(catalogue, "Screenshot_0001.png", noon + timedelta(minutes=5), camera=None)
        catalogue.set_clutter(shot, "screenshot", 0.9, {"name": 0.8})
        add(
            catalogue,
            "IMG_0002-edited.JPG",
            noon + timedelta(minutes=10),
            flags=frozenset({"edited"}),
        )
        add(
            catalogue,
            "UUID-1",
            (noon + timedelta(minutes=25)).replace(tzinfo=PARIS),
            source="icloud",
        )
    return path


def locate(path: Path, tmp_path: Path, *, apply: bool = True, timeline: object = None) -> RunResult:
    return run_stage(
        LocationStage(LocationConfig(), timeline=timeline, zone=PARIS),  # type: ignore[arg-type]
        catalogue_path=path,
        plan_dir=tmp_path / "plans",
        apply=apply,
    )


def proposals(path: Path) -> dict[str, tuple[tuple[float, float], str]]:
    with Catalogue.open(path) as catalogue:
        return {
            (catalogue.item(i).title or ""): (place, method)
            for i, (place, method, _) in catalogue.inferred_locations().items()
        }


def item_id(path: Path, source: str, key: str) -> int:
    with Catalogue.open(path) as catalogue:
        found = catalogue.item_for_key(source, key)  # type: ignore[arg-type]
        assert found is not None
        return found


def test_shots_without_gps_get_proposals_from_their_cameras_neighbours(
    catalogue_path: Path, tmp_path: Path
) -> None:
    result = locate(catalogue_path, tmp_path)

    found = proposals(catalogue_path)
    assert set(found) == {"IMG_0002.JPG", "UUID-1"}
    (lat, lon), method = found["IMG_0002.JPG"]
    assert (round(lat, 6), round(lon, 6), method) == (48.1, 2.2, "interpolated")
    assert found["UUID-1"] == ((48.2, 2.4), "neighbour")
    assert result.plan.summary() == {"propose-location": 2}


def test_proposals_are_not_written_until_approved(catalogue_path: Path, tmp_path: Path) -> None:
    locate(catalogue_path, tmp_path)
    lost = item_id(catalogue_path, "google", f"{ROOT}/IMG_0002.JPG")

    with Catalogue.open(catalogue_path) as catalogue:
        assert catalogue.item(lost).latitude is None
        assert catalogue.keywords(lost) == set()
        assert approved_location(catalogue, lost) is None
        catalogue.set_decision("item", lost, "location", {"approve": False})
        assert approved_location(catalogue, lost) is None
        catalogue.set_decision("item", lost, "location", {"approve": True})
        place = approved_location(catalogue, lost)
    assert place is not None
    assert (round(place[0], 6), round(place[1], 6)) == (48.1, 2.2)


def test_an_item_without_a_proposal_has_nothing_to_approve(
    catalogue_path: Path, tmp_path: Path
) -> None:
    locate(catalogue_path, tmp_path)
    alone = item_id(catalogue_path, "google", f"{ROOT}/IMG_0004.JPG")

    with Catalogue.open(catalogue_path) as catalogue:
        catalogue.set_decision("item", alone, "location", {"approve": True})
        assert approved_location(catalogue, alone) is None


def test_the_timeline_places_a_shot_with_no_neighbours(
    catalogue_path: Path, tmp_path: Path
) -> None:
    start = datetime(2025, 6, 1, 13, 30, tzinfo=PARIS)
    timeline = parse_timeline(
        TimelineBuilder().visit(start, start + timedelta(hours=1), 43.7, 7.26).data()
    )

    locate(catalogue_path, tmp_path, timeline=timeline)

    assert proposals(catalogue_path)["IMG_0004.JPG"] == ((43.7, 7.26), "timeline-visit")


def test_re_running_changes_nothing_and_a_dry_run_stores_nothing(
    catalogue_path: Path, tmp_path: Path
) -> None:
    dry = locate(catalogue_path, tmp_path, apply=False)
    assert dry.plan.actions
    assert proposals(catalogue_path) == {}

    locate(catalogue_path, tmp_path)
    again = locate(catalogue_path, tmp_path)

    assert again.plan.actions == ()


def test_a_proposal_that_no_longer_holds_is_retired(catalogue_path: Path, tmp_path: Path) -> None:
    locate(catalogue_path, tmp_path)
    with Catalogue.open(catalogue_path) as catalogue:
        found = catalogue.get_item("google", f"{ROOT}/IMG_0002.JPG")
        assert found is not None
        catalogue.upsert_item(found.replace(latitude=48.15, longitude=2.25))

    result = locate(catalogue_path, tmp_path)

    assert result.plan.summary() == {"retire-location": 1}
    assert set(proposals(catalogue_path)) == {"UUID-1"}


def test_an_action_for_an_item_that_is_gone_is_an_error(tmp_path: Path) -> None:
    from photomigrate.runner import Action

    action = Action("propose-location", "GONE", {"source": "icloud", "latitude": 1.0,
                                                 "longitude": 2.0, "method": "neighbour",
                                                 "evidence": {}})  # fmt: skip
    with Catalogue.snapshot(tmp_path / "none") as catalogue, pytest.raises(KeyError, match="GONE"):
        LocationStage(LocationConfig()).apply(action, catalogue)


def test_analyse_proposes_locations_with_an_optional_timeline(
    catalogue_path: Path, tmp_path: Path
) -> None:
    start = datetime(2025, 6, 1, 11, 30, tzinfo=UTC)
    export = tmp_path / "Timeline.json"
    export.write_text(
        TimelineBuilder().visit(start, start + timedelta(hours=1), 43.7, 7.26).to_json(), "utf-8"
    )
    args = ["--catalogue", str(catalogue_path), "--plan-dir", str(tmp_path / "plans")]
    runner = CliRunner()

    result = runner.invoke(
        app, [*args, "analyse", "--apply", "--timeline", str(export), "--timezone", "Europe/Paris"]
    )

    assert result.exit_code == 0, result.output
    assert proposals(catalogue_path)["IMG_0004.JPG"] == ((43.7, 7.26), "timeline-visit")
    missing = runner.invoke(app, [*args, "analyse", "--timeline", str(tmp_path / "nope.json")])
    assert missing.exit_code == 1
    assert "no Timeline export" in missing.output
    zone = runner.invoke(app, [*args, "analyse", "--timezone", "Mars/Olympus"])
    assert zone.exit_code == 2
