"""Live Photo pairing over the catalogue, after ingest (CLA-78)."""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from photomigrate.catalogue import Catalogue
from photomigrate.ingest.livephotos import LivePhotoStage
from photomigrate.ingest.stage import IngestStage
from photomigrate.runner import RunResult, run_stage
from photomigrate.testing.takeout import TakeoutBuilder, TakeoutManifest

ROOT = "Takeout/Google Photos"


def run_both(takeout: Path, tmp: Path) -> tuple[RunResult, RunResult]:
    common = {"catalogue_path": tmp / "catalogue.sqlite", "plan_dir": tmp / "plans", "apply": True}
    with IngestStage(takeout, tmp / "work", zone=ZoneInfo("Europe/Paris")) as ingest:
        first = run_stage(ingest, **common)  # type: ignore[arg-type]
    return first, run_stage(LivePhotoStage(), **common)  # type: ignore[arg-type]


def test_split_live_photos_become_pairs(
    standard_takeout_zips: TakeoutManifest, tmp_path: Path
) -> None:
    _, live = run_both(standard_takeout_zips.zips[0].parent, tmp_path)

    with Catalogue.open(tmp_path / "catalogue.sqlite") as catalogue:
        groups = catalogue.groups("live-pair")
        found = {}
        for group in groups:
            members = catalogue.group_members(group.id)
            found[group.key] = (
                [(catalogue.item(i).source_key, role) for i, role, _ in members],
                group.details["method"],
            )
            still_id, motion_id = members[0][0], members[1][0]
            assert "live-photo-still" in catalogue.item(still_id).flags
            assert "live-photo-motion" in catalogue.item(motion_id).flags
        flagged = [i.source_key for i in catalogue.items("google") if "unpaired-motion" in i.flags]

    assert found == {
        f"{ROOT}/Photos from 2019/IMG_2001.HEIC": (
            [
                (f"{ROOT}/Photos from 2019/IMG_2001.HEIC", "still"),
                (f"{ROOT}/Photos from 2019/IMG_2001.MOV", "motion"),
            ],
            "name-time",
        ),
        f"{ROOT}/Photos from 2015/IMG_2002.JPG": (
            [
                (f"{ROOT}/Photos from 2015/IMG_2002.JPG", "still"),
                (f"{ROOT}/Photos from 2015/IMG_2002.MP4", "motion"),
            ],
            "name-time",
        ),
    }
    assert flagged == [], "ordinary short videos are not motion files"
    assert live.applied == 2


def test_no_short_motion_with_a_still_is_left_standalone(
    standard_takeout_zips: TakeoutManifest, tmp_path: Path
) -> None:
    run_both(standard_takeout_zips.zips[0].parent, tmp_path)

    with Catalogue.open(tmp_path / "catalogue.sqlite") as catalogue:
        for item in standard_takeout_zips.with_quirk("live-photo-motion"):
            row = catalogue.get_item("google", item.path)
            assert row is not None
            assert "live-photo-motion" in row.flags, item.path


def test_a_motion_clip_whose_still_is_missing_is_flagged_for_review(tmp_path: Path) -> None:
    b = TakeoutBuilder(seed=11)
    taken = datetime(2019, 7, 5, 14, 20, tzinfo=UTC)
    still, motion = b.live_photo("Photos from 2019", "IMG_3001", taken=taken)
    files = b.files()
    keep = TakeoutBuilder(seed=12)
    keep.other_product(motion.path.removeprefix("Takeout/"), files[motion.path])
    keep.photo("Photos from 2019", "IMG_3002.HEIC", taken=taken)
    keep.write_zips(tmp_path / "in")

    _, live = run_both(tmp_path / "in", tmp_path)

    assert [a.kind for a in live.plan.actions] == ["flag-unpaired-motion"]
    with Catalogue.open(tmp_path / "catalogue.sqlite") as catalogue:
        row = catalogue.get_item("google", motion.path)
        assert row is not None
        assert "unpaired-motion" in row.flags
        assert row.content_id == still.content_id


def test_pairing_again_changes_nothing(
    standard_takeout_zips: TakeoutManifest, tmp_path: Path
) -> None:
    run_both(standard_takeout_zips.zips[0].parent, tmp_path)
    before = (tmp_path / "catalogue.sqlite").read_bytes()

    again = run_stage(
        LivePhotoStage(),
        catalogue_path=tmp_path / "catalogue.sqlite",
        plan_dir=tmp_path / "plans",
        apply=True,
    )

    assert again.applied == 0
    assert (tmp_path / "catalogue.sqlite").read_bytes() == before
