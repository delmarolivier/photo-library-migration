"""Stage runner: every stage plans first, and only ``--apply`` executes the plan.

A stage turns inputs plus catalogue state into ``Action``s, and applies one action at a time.
The runner makes stages safe to re-run:

* Dry run (the default): plan against an in-memory snapshot of the catalogue and save the plan
  as JSON. Nothing else is written.
* Apply: skip actions already applied in earlier runs (by key), apply each remaining one in
  its own catalogue transaction, and log it. A failure stops the run. The next run resumes
  with the failed action. A stage with nothing left to do changes nothing at all.

``Stage.apply`` must be idempotent on the filesystem, because an action whose catalogue
transaction failed is applied again.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from rich.console import Console
from rich.table import Table

from photomigrate.catalogue import Catalogue, Clock, utc_now

type JSONValue = str | int | float | bool | list[JSONValue] | dict[str, JSONValue] | None
Mode = Literal["dry-run", "apply"]

DEFAULT_CATALOGUE = Path("data/catalogue.sqlite")
DEFAULT_PLAN_DIR = Path("data/plans")


@dataclass(frozen=True)
class Action:
    """One planned change. ``key`` identifies it across runs: same inputs, same key."""

    kind: str
    target: str
    details: Mapping[str, JSONValue] = field(default_factory=dict)

    @property
    def key(self) -> str:
        canonical = json.dumps(
            [self.kind, self.target, self.details], sort_keys=True, ensure_ascii=False
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        return f"{self.kind}:{self.target}#{digest}"

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "key": self.key,
            "kind": self.kind,
            "target": self.target,
            "details": dict(self.details),
        }


class Stage(Protocol):
    name: str

    def plan(self, catalogue: Catalogue) -> Iterable[Action]:
        """Work out what to do. Must not write anything."""
        ...

    def apply(self, action: Action, catalogue: Catalogue) -> None:
        """Carry out one action. Must be idempotent."""
        ...


@dataclass(frozen=True)
class Plan:
    stage: str
    mode: Mode
    created_at: datetime
    actions: tuple[Action, ...]
    already_applied: int

    def summary(self) -> dict[str, int]:
        return dict(Counter(action.kind for action in self.actions))

    def to_json(self) -> str:
        return json.dumps(
            {
                "stage": self.stage,
                "mode": self.mode,
                "created_at": self.created_at.isoformat(),
                "already_applied": self.already_applied,
                "summary": self.summary(),
                "actions": [action.to_dict() for action in self.actions],
            },
            ensure_ascii=False,
            indent=2,
        )

    def save(self, plan_dir: Path) -> Path:
        plan_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{self.stage}-{self.created_at:%Y%m%dT%H%M%SZ}-{self.mode}"
        path = plan_dir / f"{stem}.json"
        counter = 1
        while path.exists():
            counter += 1
            path = plan_dir / f"{stem}-{counter}.json"
        path.write_text(self.to_json(), encoding="utf-8")
        return path


@dataclass(frozen=True)
class RunResult:
    plan: Plan
    plan_path: Path | None
    applied: int


class StageFailedError(RuntimeError):
    def __init__(self, stage: str, action: Action, cause: Exception) -> None:
        super().__init__(f"{stage}: {action.kind} {action.target} failed: {cause}")
        self.stage = stage
        self.action = action


def _plan(stage: Stage, catalogue: Catalogue, mode: Mode, created_at: datetime) -> Plan:
    history = catalogue.applied_actions(stage.name)
    planned: dict[str, Action] = {}
    for action in stage.plan(catalogue):
        planned.setdefault(action.key, action)

    def done(action: Action) -> bool:
        """Applied, and not undone since by an action on the same target that the stage no
        longer plans (a setting changed, then changed back)."""
        applied = history.get(action.target, {})
        when = applied.get(action.key)
        return when is not None and all(
            later <= when or key in planned for key, later in applied.items()
        )

    pending = tuple(action for action in planned.values() if not done(action))
    return Plan(stage.name, mode, created_at, pending, len(planned) - len(pending))


def run_stage(
    stage: Stage,
    *,
    catalogue_path: Path = DEFAULT_CATALOGUE,
    plan_dir: Path = DEFAULT_PLAN_DIR,
    apply: bool = False,
    clock: Clock = utc_now,
) -> RunResult:
    created_at = clock()
    if not apply:
        with Catalogue.snapshot(catalogue_path, clock=clock) as snapshot:
            plan = _plan(stage, snapshot, "dry-run", created_at)
        return RunResult(plan, plan.save(plan_dir), applied=0)

    with Catalogue.open(catalogue_path, clock=clock) as catalogue:
        plan = _plan(stage, catalogue, "apply", created_at)
        if not plan.actions:
            return RunResult(plan, None, applied=0)
        plan_path = plan.save(plan_dir)
        run_id = catalogue.start_run(stage.name, str(plan_path))
        for seq, action in enumerate(plan.actions, start=1):
            catalogue.log_action(
                run_id, seq, key=action.key, kind=action.kind, target=action.target,
                details=json.dumps(dict(action.details), ensure_ascii=False, sort_keys=True),
            )  # fmt: skip
            try:
                with catalogue.transaction():
                    stage.apply(action, catalogue)
                    catalogue.set_action_status(run_id, seq, "applied")
            except Exception as exc:
                catalogue.set_action_status(run_id, seq, "failed", f"{type(exc).__name__}: {exc}")
                catalogue.finish_run(run_id, "failed")
                raise StageFailedError(stage.name, action, exc) from exc
        catalogue.finish_run(run_id, "completed")
    return RunResult(plan, plan_path, applied=len(plan.actions))


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def render_result(result: RunResult, console: Console, *, limit: int = 50) -> None:
    """Print the plan as tables, then what happened and what to do next."""
    plan = result.plan
    if not plan.actions:
        console.print(
            f"[bold]{plan.stage}[/]: Nothing to do "
            f"({_plural(plan.already_applied, 'action')} already applied)."
        )
        return
    summary = Table(title=f"{plan.stage}: {_plural(len(plan.actions), 'action')}")
    summary.add_column("Action")
    summary.add_column("Count", justify="right")
    for kind, count in sorted(plan.summary().items()):
        summary.add_row(kind, str(count))
    console.print(summary)

    actions = Table(show_lines=False)
    for column in ("#", "Action", "Target", "Details"):
        actions.add_column(column, overflow="fold")
    for seq, action in enumerate(plan.actions[:limit], start=1):
        details = json.dumps(dict(action.details), ensure_ascii=False, sort_keys=True)
        actions.add_row(str(seq), action.kind, action.target, details)
    console.print(actions)
    if len(plan.actions) > limit:
        console.print(f"… and {len(plan.actions) - limit} more (see the plan JSON).")

    console.print(f"Plan saved to {result.plan_path}")
    if plan.mode == "dry-run":
        console.print("Dry run: nothing was changed. Re-run with [bold]--apply[/] to do it.")
    else:
        console.print(f"Applied {_plural(result.applied, 'action')}.")
