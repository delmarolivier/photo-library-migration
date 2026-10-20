"""edited-versions: link each Takeout -edited copy to its original, under a policy (CLA-82)."""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from photomigrate.analyse.edited import EditedVersionsStage
from typer.testing import CliRunner

from photomigrate.analyse.dedupe_photo import DedupeStage
from photomigrate.catalogue import Catalogue
from photomigrate.cli import app
from photomigrate.ingest.stage import IngestStage
from photomigrate.runner import RunResult, run_stage
from photomigrate.testing.takeout import TakeoutBuilder, TakeoutManifest


def ingest(takeout: Path, tmp_path: Path) -> Path:
    path = tmp_path / "catalogue.sqlite"
    with IngestStage(takeout, tmp_path / "work", zone=ZoneInfo("Europe/Paris")) as stage:
        run_stage(stage, catalogue_path=path, plan_dir=tmp_path / "plans", apply=True)
    return path


@pytest.fixture
def catalogue_path(standard_takeout_zips: TakeoutManifest, tmp_path: Path) -> Path:
    return ingest(standard_takeout_zips.zips[0].parent, tmp_path)


def pair_edits(path: Path, tmp_path: Path, policy: str = "pair") -> RunResult:
    return run_stage(
        EditedVersionsStage(policy),  # type: ignore[arg-type]
        catalogue_path=path,
        plan_dir=tmp_path / "plans",
        apply=True,
    )


def pairs(path: Path) -> dict[str, tuple[str, list[str], str]]:
    """edited key -> (original key, keep list, policy)."""
    with Catalogue.open(path) as catalogue:
        found = {}
        for group in catalogue.groups("edit-pair"):
            members = {
                role: catalogue.item(i).source_key
                for i, role, _ in catalogue.group_members(group.id)
            }
            assert members["edited"] == group.key
            found[group.key] = (members["original"], group.details["keep"], group.details["policy"])
        return found


def test_every_edit_is_linked_to_its_true_original(
    catalogue_path: Path, standard_takeout_zips: TakeoutManifest, tmp_path: Path
) -> None:
    pair_edits(catalogue_path, tmp_path)

    expected = {i.path: i.edit_of for i in standard_takeout_zips.with_quirk("edited")}
    assert {edit: original for edit, (original, _, _) in pairs(catalogue_path).items()} == expected
    assert len(expected) == 7
    with Catalogue.open(catalogue_path) as catalogue:
        original = catalogue.get_item("google", next(iter(expected.values())) or "")
        assert original is not None
        assert "has-edit" in original.flags


@pytest.mark.parametrize(
    ("policy", "kept"),
    [
        ("pair", {"original", "edited"}),
        ("original-only", {"original"}),
        ("edited-only", {"edited"}),
    ],
)
def test_each_policy_says_what_to_keep(
    catalogue_path: Path, tmp_path: Path, policy: str, kept: set[str]
) -> None:
    pair_edits(catalogue_path, tmp_path, policy)

    for edited, (original, keep, used) in pairs(catalogue_path).items():
        assert used == policy
        assert set(keep) == {
            k for role, k in (("original", original), ("edited", edited)) if role in kept
        }


def test_switching_policy_updates_every_pair(catalogue_path: Path, tmp_path: Path) -> None:
    pair_edits(catalogue_path, tmp_path, "pair")

    switched = pair_edits(catalogue_path, tmp_path, "original-only")

    assert switched.applied == 7
    assert {policy for _, _, policy in pairs(catalogue_path).values()} == {"original-only"}
    assert pair_edits(catalogue_path, tmp_path, "original-only").applied == 0


def test_an_edit_whose_original_is_missing_is_flagged(tmp_path: Path) -> None:
    b = TakeoutBuilder(seed=21)
    original = b.photo("Photos from 2019", "IMG_9.JPG", taken=datetime(2019, 7, 1, tzinfo=UTC))
    edit = b.edited(original)
    files = b.files()
    lone = TakeoutBuilder(seed=22)
    lone.other_product(edit.path.removeprefix("Takeout/"), files[edit.path])
    lone.write_zips(tmp_path / "in")
    path = ingest(tmp_path / "in", tmp_path)

    result = pair_edits(path, tmp_path)

    assert [a.kind for a in result.plan.actions] == ["flag-unpaired-edit"]
    with Catalogue.open(path) as catalogue:
        row = catalogue.get_item("google", edit.path)
        assert row is not None
        assert "unpaired-edit" in row.flags


def test_an_edit_and_its_original_are_never_duplicates(
    catalogue_path: Path, tmp_path: Path
) -> None:
    pair_edits(catalogue_path, tmp_path)
    run_stage(DedupeStage(), catalogue_path=catalogue_path, plan_dir=tmp_path / "plans", apply=True)

    edits = set(pairs(catalogue_path))
    with Catalogue.open(catalogue_path) as catalogue:
        for group in catalogue.groups("duplicate"):
            keys = {catalogue.item(i).source_key for i, _, _ in catalogue.group_members(group.id)}
            assert not keys & edits


def test_analyse_takes_the_policy(catalogue_path: Path, tmp_path: Path) -> None:
    args = ["--catalogue", str(catalogue_path), "--plan-dir", str(tmp_path / "plans"), "analyse"]

    result = CliRunner().invoke(app, [*args, "--apply", "--edited-policy", "edited-only"])

    assert result.exit_code == 0, result.output
    assert "edited-versions" in result.output
    assert {policy for _, _, policy in pairs(catalogue_path).values()} == {"edited-only"}
    bad = CliRunner().invoke(app, [*args, "--edited-policy", "both"])
    assert bad.exit_code == 2
