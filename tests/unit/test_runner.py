"""The stage runner: dry-run plans by default, --apply executes, re-runs and resumes safely."""

import hashlib
import io
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from photomigrate.runner import Action, Plan, StageFailedError, render_result, run_stage
from rich.console import Console

from photomigrate.catalogue import Catalogue, CatalogueItem

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class CopyStage:
    """Copies source files into a work dir and catalogues them; idempotent per action."""

    source: Path
    work: Path
    fail_on: str | None = None
    fail_after_catalogue_write: bool = False
    name: str = "copy"

    def plan(self, catalogue: Catalogue) -> Iterator[Action]:
        for file in sorted(self.source.rglob("*")):
            if file.is_file():
                yield Action(
                    "copy", file.relative_to(self.source).as_posix(), {"sha256": sha(file)}
                )

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        catalogue.upsert_item(
            CatalogueItem(source="google", source_key=action.target, kind="photo",
                          sha256=str(action.details["sha256"]))
        )  # fmt: skip
        if action.target == self.fail_on:
            raise OSError("disk full")
        dest = self.work / action.target
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = (self.source / action.target).read_bytes()
        if not dest.exists() or dest.read_bytes() != data:
            dest.write_bytes(data)


def tree(root: Path) -> dict[str, tuple[str, int]]:
    return {
        p.relative_to(root).as_posix(): (sha(p), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def rows(db: Path, sql: str) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, CopyStage, Path, Path]:
    source = tmp_path / "in"
    (source / "sub").mkdir(parents=True)
    for name in ("a.jpg", "b.jpg", "sub/c.jpg"):
        (source / name).write_bytes(name.encode())
    return (
        tmp_path,
        CopyStage(source, tmp_path / "work"),
        tmp_path / "data" / "catalogue.sqlite",
        tmp_path / "data" / "plans",
    )


def run(stage: CopyStage, db: Path, plans: Path, *, apply: bool, clock: Clock | None = None):  # type: ignore[no-untyped-def]
    return run_stage(stage, catalogue_path=db, plan_dir=plans, apply=apply, clock=clock or Clock())


def test_dry_run_only_writes_the_plan(setup: tuple[Path, CopyStage, Path, Path]) -> None:
    root, stage, db, plans = setup
    before = tree(root)

    result = run(stage, db, plans, apply=False)

    after = tree(root)
    assert result.plan_path is not None
    assert set(after) - set(before) == {result.plan_path.relative_to(root).as_posix()}
    assert {k: v for k, v in after.items() if k in before} == before
    assert not db.exists()
    assert not stage.work.exists()
    assert [a.target for a in result.plan.actions] == ["a.jpg", "b.jpg", "sub/c.jpg"]
    assert result.applied == 0


def test_dry_run_plans_against_the_real_catalogue_without_touching_it(
    setup: tuple[Path, CopyStage, Path, Path],
) -> None:
    root, stage, db, plans = setup
    run(stage, db, plans, apply=True)
    (stage.source / "d.jpg").write_bytes(b"new")
    before = tree(root)

    result = run(stage, db, plans, apply=False)

    assert [a.target for a in result.plan.actions] == ["d.jpg"]
    assert result.plan.already_applied == 3
    assert result.plan_path is not None
    assert set(tree(root)) - set(before) == {result.plan_path.relative_to(root).as_posix()}
    assert {k: v for k, v in tree(root).items() if k in before} == before


def test_plan_json_lists_every_action(setup: tuple[Path, CopyStage, Path, Path]) -> None:
    _, stage, db, plans = setup

    result = run(stage, db, plans, apply=False)

    assert result.plan_path is not None
    data = json.loads(result.plan_path.read_text())
    assert data["stage"] == "copy"
    assert data["mode"] == "dry-run"
    assert data["created_at"] == "2026-10-20T09:00:01+00:00"
    assert data["summary"] == {"copy": 3}
    assert data["already_applied"] == 0
    first = data["actions"][0]
    assert first["kind"] == "copy"
    assert first["target"] == "a.jpg"
    assert first["details"] == {"sha256": sha(stage.source / "a.jpg")}
    assert first["key"] == Action("copy", "a.jpg", {"sha256": sha(stage.source / "a.jpg")}).key


def test_apply_executes_records_progress_and_saves_the_plan(
    setup: tuple[Path, CopyStage, Path, Path],
) -> None:
    _, stage, db, plans = setup

    result = run(stage, db, plans, apply=True)

    assert result.applied == 3
    assert result.plan.mode == "apply"
    assert result.plan_path is not None
    assert result.plan_path.exists()
    assert (stage.work / "sub" / "c.jpg").read_bytes() == b"sub/c.jpg"
    assert rows(db, "SELECT stage, status FROM stage_runs") == [("copy", "completed")]
    assert rows(db, "SELECT seq, target, status FROM actions ORDER BY seq") == [
        (1, "a.jpg", "applied"),
        (2, "b.jpg", "applied"),
        (3, "sub/c.jpg", "applied"),
    ]
    assert [r[0] for r in rows(db, "SELECT source_key FROM items ORDER BY id")] == [
        "a.jpg",
        "b.jpg",
        "sub/c.jpg",
    ]


def test_re_running_a_completed_stage_changes_nothing(
    setup: tuple[Path, CopyStage, Path, Path],
) -> None:
    root, stage, db, plans = setup
    run(stage, db, plans, apply=True)
    before = tree(root)

    result = run(stage, db, plans, apply=True)

    assert tree(root) == before
    assert result.applied == 0
    assert result.plan_path is None
    assert result.plan.already_applied == 3


def test_a_failed_run_resumes_where_it_stopped(setup: tuple[Path, CopyStage, Path, Path]) -> None:
    _, stage, db, plans = setup
    stage.fail_on = "b.jpg"

    with pytest.raises(StageFailedError, match=r"copy.*b\.jpg.*disk full"):
        run(stage, db, plans, apply=True)

    assert (stage.work / "a.jpg").exists()
    assert not (stage.work / "b.jpg").exists()
    assert rows(db, "SELECT status FROM stage_runs") == [("failed",)]
    assert rows(db, "SELECT target, status, error FROM actions ORDER BY seq") == [
        ("a.jpg", "applied", None),
        ("b.jpg", "failed", "OSError: disk full"),
    ]
    assert rows(db, "SELECT source_key FROM items") == [("a.jpg",)], (
        "b's catalogue write rolled back"
    )

    stage.fail_on = None
    result = run(stage, db, plans, apply=True)

    assert [a.target for a in result.plan.actions] == ["b.jpg", "sub/c.jpg"]
    assert result.applied == 2
    assert rows(db, "SELECT status FROM stage_runs ORDER BY id") == [("failed",), ("completed",)]


def test_changed_inputs_are_applied_again(setup: tuple[Path, CopyStage, Path, Path]) -> None:
    _, stage, db, plans = setup
    run(stage, db, plans, apply=True)
    (stage.source / "a.jpg").write_bytes(b"retouched")

    result = run(stage, db, plans, apply=True)

    assert [a.target for a in result.plan.actions] == ["a.jpg"]
    assert (stage.work / "a.jpg").read_bytes() == b"retouched"


def test_duplicate_actions_in_a_plan_run_once(setup: tuple[Path, CopyStage, Path, Path]) -> None:
    _, stage, db, plans = setup

    class Twice(CopyStage):
        def plan(self, catalogue: Catalogue) -> Iterator[Action]:
            for action in super().plan(catalogue):
                yield action
                yield action

    result = run(Twice(stage.source, stage.work), db, plans, apply=True)

    assert result.applied == 3


def test_action_keys_depend_on_kind_target_and_details() -> None:
    base = Action("copy", "a.jpg", {"sha256": "1"})

    assert base.key == Action("copy", "a.jpg", {"sha256": "1"}).key
    assert len({base.key, Action("move", "a.jpg", {"sha256": "1"}).key,
                Action("copy", "b.jpg", {"sha256": "1"}).key,
                Action("copy", "a.jpg", {"sha256": "2"}).key}) == 4  # fmt: skip
    assert base.key.startswith("copy:a.jpg#")


def test_plans_saved_in_the_same_second_do_not_overwrite_each_other(
    setup: tuple[Path, CopyStage, Path, Path],
) -> None:
    _, stage, db, plans = setup

    class Frozen(Clock):
        def __call__(self) -> datetime:
            return T0

    first = run(stage, db, plans, apply=False, clock=Frozen())
    second = run(stage, db, plans, apply=False, clock=Frozen())

    assert first.plan_path != second.plan_path
    assert first.plan_path is not None
    assert first.plan_path.exists()


def render(result: object, limit: int = 50) -> str:
    out = io.StringIO()
    render_result(result, Console(file=out, width=120, color_system=None), limit=limit)  # type: ignore[arg-type]
    return out.getvalue()


def test_dry_run_output_shows_a_table_and_how_to_apply(
    setup: tuple[Path, CopyStage, Path, Path],
) -> None:
    _, stage, db, plans = setup

    text = render(run(stage, db, plans, apply=False))

    assert "copy" in text
    assert "sub/c.jpg" in text
    assert "3 actions" in text
    assert "--apply" in text
    assert "Plan saved to" in text


def test_long_plans_are_truncated_in_the_table(setup: tuple[Path, CopyStage, Path, Path]) -> None:
    _, stage, db, plans = setup

    text = render(run(stage, db, plans, apply=False), limit=2)

    assert "b.jpg" in text
    assert "sub/c.jpg" not in text
    assert "1 more" in text


def test_apply_output_reports_what_was_done(setup: tuple[Path, CopyStage, Path, Path]) -> None:
    _, stage, db, plans = setup

    assert "Applied 3 actions" in render(run(stage, db, plans, apply=True))
    assert "Nothing to do" in render(run(stage, db, plans, apply=True))


def test_plan_summary_counts_by_kind() -> None:
    plan = Plan(
        stage="s",
        mode="dry-run",
        created_at=T0,
        actions=(Action("copy", "a"), Action("copy", "b"), Action("tag", "a")),
        already_applied=0,
    )

    assert plan.summary() == {"copy": 2, "tag": 1}
