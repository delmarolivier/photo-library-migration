"""The dedupe-photos stage over a real catalogue: Google ↔ iCloud and within Google (CLA-80)."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from photomigrate.analyse.dedupe_photo import DedupeConfig, DedupeStage
from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.cli import app
from photomigrate.ingest.livephotos import LivePhotoStage
from photomigrate.ingest.stage import IngestStage
from photomigrate.runner import RunResult, run_stage
from photomigrate.testing.takeout import TakeoutManifest

ROOT = "Takeout/Google Photos"
BEACH = f"{ROOT}/Photos from 2019/IMG_1001.JPG"


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


def icloud_copy(catalogue: Catalogue, google_key: str, uuid: str, **changes: object) -> int:
    """An iCloud item holding the same picture as a catalogued Google item."""
    google = catalogue.get_item("google", google_key)
    assert google is not None
    assert google.id is not None
    item = CatalogueItem(
        source="icloud", source_key=uuid, kind="photo", sha256=google.sha256, phash=google.phash,
        dhash=google.dhash, phash_fine=google.phash_fine, width=google.width, height=google.height,
        title=google.title,
    ).replace(**changes)  # fmt: skip
    item_id = catalogue.upsert_item(item)
    exif = catalogue.date_candidates(google.id).get("exif")
    if exif is not None:
        catalogue.set_date_candidate(item_id, "exif", exif)
    return item_id


def dedupe(path: Path, tmp_path: Path, config: DedupeConfig | None = None) -> RunResult:
    return run_stage(
        DedupeStage(config or DedupeConfig()),
        catalogue_path=path,
        plan_dir=tmp_path / "plans",
        apply=True,
    )


def proposals(path: Path) -> dict[str, list[tuple[str, str, str | None]]]:
    with Catalogue.open(path) as catalogue:
        found = {}
        for group in catalogue.groups("duplicate"):
            members = []
            for item_id, role, _ in catalogue.group_members(group.id):
                item = catalogue.item(item_id)
                members.append((item.source, item.source_key, role))
            found[group.details["keeper"]] = members
        return found


def test_google_near_duplicates_and_icloud_copies_are_proposed_as_groups(
    catalogue_path: Path, tmp_path: Path
) -> None:
    with Catalogue.open(catalogue_path) as catalogue:
        icloud_copy(catalogue, BEACH, "UUID-BEACH")

    dedupe(catalogue_path, tmp_path)

    found = proposals(catalogue_path)
    assert found[f"google:{BEACH}"] == [
        ("google", BEACH, "keeper"),
        # Full size, and it kept the original's EXIF including GPS: richer than the bare copy.
        ("google", f"{ROOT}/Photos from 2019/received_1001.jpeg", "duplicate"),
        ("icloud", "UUID-BEACH", "duplicate"),
        ("google", f"{ROOT}/Photos from 2019/IMG-20190702-WA0003.jpg", "duplicate"),
    ]
    assert found[f"google:{ROOT}/Photos from 2019/IMG_1003.HEIC"] == [
        ("google", f"{ROOT}/Photos from 2019/IMG_1003.HEIC", "keeper"),
        ("google", f"{ROOT}/Exports/IMG_1003.jpg", "duplicate"),
    ]
    assert len(found) == 2, "edits, Google creations and distinct photos are not duplicates"


def test_the_icloud_copy_wins_when_it_is_as_good(catalogue_path: Path, tmp_path: Path) -> None:
    with Catalogue.open(catalogue_path) as catalogue:
        icloud_copy(
            catalogue, BEACH, "UUID-BEACH", latitude=43.7, longitude=7.27, description="Nice"
        )

    dedupe(catalogue_path, tmp_path)

    assert "icloud:UUID-BEACH" in proposals(catalogue_path)


def test_a_duplicate_live_photo_brings_its_motion_clip(
    catalogue_path: Path, tmp_path: Path
) -> None:
    still = f"{ROOT}/Photos from 2019/IMG_2001.HEIC"
    with Catalogue.open(catalogue_path) as catalogue:
        icloud_copy(catalogue, still, "UUID-LIVE", latitude=1.0, longitude=2.0, description="x")

    dedupe(catalogue_path, tmp_path)

    assert proposals(catalogue_path)["icloud:UUID-LIVE"] == [
        ("icloud", "UUID-LIVE", "keeper"),
        ("google", still, "duplicate"),
        ("google", f"{ROOT}/Photos from 2019/IMG_2001.MOV", "motion"),
    ]


def test_dedupe_again_changes_nothing_and_stale_groups_are_retired(
    catalogue_path: Path, tmp_path: Path
) -> None:
    dedupe(catalogue_path, tmp_path)
    before = (catalogue_path).read_bytes()

    assert dedupe(catalogue_path, tmp_path).applied == 0
    assert catalogue_path.read_bytes() == before

    result = dedupe(catalogue_path, tmp_path, DedupeConfig(candidate_phash=-1))

    assert {a.kind for a in result.plan.actions} == {"retire-duplicates"}
    assert proposals(catalogue_path) == {}


def test_proposals_never_record_a_decision(catalogue_path: Path, tmp_path: Path) -> None:
    dedupe(catalogue_path, tmp_path)

    with Catalogue.open(catalogue_path) as catalogue:
        assert catalogue._conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0


def test_analyse_is_a_dry_run_by_default(catalogue_path: Path, tmp_path: Path) -> None:
    args = ["--catalogue", str(catalogue_path), "--plan-dir", str(tmp_path / "plans"), "analyse"]

    dry = CliRunner().invoke(app, args)
    assert dry.exit_code == 0, dry.output
    assert "propose-duplicates" in dry.output
    assert "Dry run" in dry.output
    assert proposals(catalogue_path) == {}

    wet = CliRunner().invoke(app, [*args, "--apply", "--fine-max", "12", "--dhash-max", "10"])
    assert wet.exit_code == 0, wet.output
    assert len(proposals(catalogue_path)) == 2


def test_items_without_hashes_are_left_out(catalogue_path: Path, tmp_path: Path) -> None:
    with Catalogue.open(catalogue_path) as catalogue:
        catalogue.upsert_item(
            CatalogueItem(source="icloud", source_key="UUID-MISSING", kind="photo")
        )

    result = dedupe(catalogue_path, tmp_path)

    assert all("UUID-MISSING" not in str(a.details) for a in result.plan.actions)


def test_a_proposal_for_items_that_are_gone_is_an_error(tmp_path: Path) -> None:
    from photomigrate.runner import Action

    action = Action(
        "propose-duplicates",
        "dup-x",
        {"keeper": "x", "method": "near", "members": [["icloud", "UUID-GONE", "keeper"]]},
    )
    with (
        Catalogue.snapshot(tmp_path / "none") as catalogue,
        pytest.raises(KeyError, match="UUID-GONE"),
    ):
        DedupeStage().apply(action, catalogue)


def test_discarding_a_group_that_does_not_exist_is_harmless(tmp_path: Path) -> None:
    with Catalogue.snapshot(tmp_path / "none") as catalogue:
        catalogue.discard_group("duplicate", "dup-nope")
        assert catalogue.groups("duplicate") == []


def test_analyse_reports_a_failing_run(
    catalogue_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: DedupeStage, action: object, catalogue: object) -> None:
        raise OSError("disk unplugged")

    monkeypatch.setattr(DedupeStage, "apply", broken)
    args = ["--catalogue", str(catalogue_path), "--plan-dir", str(tmp_path / "plans")]

    result = CliRunner().invoke(app, [*args, "analyse", "--apply"])

    assert result.exit_code == 1
    assert "disk unplugged" in result.output


def test_a_live_pair_group_missing_its_motion_is_tolerated(
    catalogue_path: Path, tmp_path: Path
) -> None:
    with Catalogue.open(catalogue_path) as catalogue:
        still = catalogue.item_for_key("google", BEACH)
        assert still is not None
        group = catalogue.upsert_group("live-pair", "odd", {})
        catalogue.set_group_members(group, [(still, "still", 0)])
        icloud_copy(catalogue, BEACH, "UUID-BEACH")

    dedupe(catalogue_path, tmp_path)

    assert all(role != "motion" for _, _, role in proposals(catalogue_path)[f"google:{BEACH}"])
