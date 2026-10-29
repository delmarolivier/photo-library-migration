"""`photomigrate accept`: record every unanswered proposal as the review's answer."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from photomigrate.catalogue import Catalogue, CatalogueItem
from photomigrate.cli import app
from photomigrate.review.accept import AcceptStage
from photomigrate.runner import run_stage

ROOT = "Takeout/Google Photos/Photos from 2014"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    path = tmp_path / "catalogue.sqlite"
    with Catalogue.open(path) as cat:
        a, b = (google(cat, f"IMG_{n}.JPG") for n in (1, 2))
        original, edit = google(cat, "IMG_3.JPG"), google(cat, "IMG_3-edited.JPG")
        junk, blurry = google(cat, "shot.png"), google(cat, "IMG_4.JPG")
        group(cat, "best-shot", "bs", [(a, "keep"), (b, "to-delete")], keep=[name(cat, a)])
        group(cat, "edit-pair", "ep", [(original, "original"), (edit, "edited")],
              policy="pair", keep=[f"{ROOT}/IMG_3.JPG", f"{ROOT}/IMG_3-edited.JPG"])  # fmt: skip
        cat.set_clutter(junk, "screenshot", 0.9)
        cat.set_clutter(b, "meme", 0.8)
        cat.set_decision("item", b, "clutter", {"reject": False})  # answered: left alone
        cat.set_quality_flags(blurry, {"blurry": 0.4})
        group(cat, "date-batch", "db", [(a, "doubtful")], problem="doubtful", camera=None,
              folder="f", prefix="IMG_", current={}, suggestions={})  # fmt: skip
    return path


def google(cat: Catalogue, name: str) -> int:
    return cat.upsert_item(
        CatalogueItem(source="google", source_key=f"{ROOT}/{name}", kind="photo")
    )


def name(cat: Catalogue, item_id: int) -> str:
    item = cat.item(item_id)
    return f"{item.source}:{item.source_key}"


def group(
    cat: Catalogue, kind: str, key: str, members: list[tuple[int, str]], **details: object
) -> int:
    group_id = cat.upsert_group(kind, key, details)  # type: ignore[arg-type]
    cat.set_group_members(group_id, [(i, role, rank) for rank, (i, role) in enumerate(members)])
    return group_id


QUESTIONS = {
    "bs": ("group", "best-shot"),
    "ep": ("group", "edit-pair"),
    "db": ("group", "date"),
    "shot.png": ("item", "clutter"),
    "IMG_2.JPG": ("item", "clutter"),
    "IMG_4.JPG": ("item", "quality"),
}


def answers(path: Path) -> dict[str, object]:
    """Each question's recorded answer, by group key or file name (unanswered left out)."""
    with Catalogue.open(path) as cat:
        ids = {i.source_key.rsplit("/", 1)[-1]: int(i.id or 0) for i in cat.items()}
        for kind in ("best-shot", "edit-pair", "date-batch"):
            ids |= {g.key: g.id for g in cat.groups(kind)}  # type: ignore[arg-type]
        found = {
            name: cat.decision(subject, ids[name], kind)  # type: ignore[arg-type]
            for name, (subject, kind) in QUESTIONS.items()
        }
    return {name: value for name, value in found.items() if value is not None}


def run(path: Path, tmp_path: Path, stage: AcceptStage, *, apply: bool) -> None:
    run_stage(stage, catalogue_path=path, plan_dir=tmp_path / "plans", apply=apply)


def test_the_proposals_are_recorded_with_one_version_per_edit_and_no_flagged_shots(
    path: Path, tmp_path: Path
) -> None:
    run(path, tmp_path, AcceptStage(), apply=True)

    assert answers(path) == {
        "bs": {"keep": [f"google:{ROOT}/IMG_1.JPG"]},
        "ep": {"keep": [f"{ROOT}/IMG_3-edited.JPG"]},
        "shot.png": {"reject": True},
        "IMG_2.JPG": {"reject": False},
        "IMG_4.JPG": {"reject": True},
    }, "dates and places are left to the review"


def test_the_proposals_can_be_taken_as_they_are(path: Path, tmp_path: Path) -> None:
    run(path, tmp_path, AcceptStage(edit_only=False, drop_flagged=False), apply=True)

    found = answers(path)
    assert found["ep"] == {"keep": [f"{ROOT}/IMG_3.JPG", f"{ROOT}/IMG_3-edited.JPG"]}
    assert found["IMG_4.JPG"] == {"reject": False}


def test_a_dry_run_records_nothing_and_a_second_run_finds_nothing_to_do(
    path: Path, tmp_path: Path
) -> None:
    run(path, tmp_path, AcceptStage(), apply=False)
    assert "bs" not in answers(path)

    run(path, tmp_path, AcceptStage(), apply=True)
    with Catalogue.open(path) as cat:
        assert list(AcceptStage().plan(cat)) == []


def test_the_command_is_a_dry_run_unless_applied(path: Path, tmp_path: Path) -> None:
    base = ["--catalogue", str(path), "--plan-dir", str(tmp_path / "plans"), "accept"]

    dry = CliRunner().invoke(app, base)
    assert dry.exit_code == 0, dry.output
    assert "bs" not in answers(path)

    done = CliRunner().invoke(app, [*base, "--keep-pairs", "--apply"])
    assert done.exit_code == 0, done.output
    assert answers(path)["ep"] == {"keep": [f"{ROOT}/IMG_3.JPG", f"{ROOT}/IMG_3-edited.JPG"]}
    assert answers(path)["IMG_4.JPG"] == {"reject": True}


def test_an_answer_given_after_the_plan_is_kept(path: Path) -> None:
    with Catalogue.open(path) as cat:
        stage = AcceptStage()
        action = next(a for a in stage.plan(cat) if a.details["kind"] == "best-shot")
        cat.set_decision("group", int(action.details["subject_id"]), "best-shot", {"keep": []})  # type: ignore[arg-type]

        stage.apply(action, cat)

    assert answers(path)["bs"] == {"keep": []}
