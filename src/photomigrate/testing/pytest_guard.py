"""Pytest plugin enforcing CLAUDE.md rule 4: tests never touch real user media.

While the session runs, any access to ``~/Pictures``, a ``*.photoslibrary`` outside tmp, a
real Takeout export, or the real samples in ``<rootdir>/data-in/`` is blocked. The test that caused it fails (or errors, from a fixture) even
if it swallows the exception, and the session fails if an access happens outside any test.
Extra protected paths can be listed in ``PHOTOMIGRATE_GUARD_FORBID`` (``os.pathsep``-separated).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from photomigrate.safety import GuardRules, PathGuard, Violation

FORBID_ENV = "PHOTOMIGRATE_GUARD_FORBID"

_guard_key = pytest.StashKey[PathGuard]()


def pytest_configure(config: pytest.Config) -> None:
    allowed = [Path(tempfile.gettempdir()), config.rootpath / "tests" / "fixtures"]
    if config.option.basetemp:
        allowed.append(Path(config.option.basetemp))
    extra = [Path(p) for p in os.environ.get(FORBID_ENV, "").split(os.pathsep) if p]
    extra.append(config.rootpath / "data-in")  # Delmar's real Takeout and Timeline samples
    guard = PathGuard(GuardRules.for_current_user(allowed_roots=allowed, extra_forbidden=extra))
    guard.activate()
    config.stash[_guard_key] = guard


def pytest_unconfigure(config: pytest.Config) -> None:
    config.stash[_guard_key].deactivate()


def _guarded(item: pytest.Item) -> Generator[None]:
    guard = item.config.stash[_guard_key]
    seen = len(guard.violations)
    try:
        yield
    finally:
        new = guard.violations[seen:]
        if new:
            pytest.fail(
                "photomigrate safety guard: this test touched real user media "
                f"(CLAUDE.md rule 4): {_describe(new)}",
                pytrace=False,
            )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None]:
    yield from _guarded(item)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None]:
    yield from _guarded(item)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None]:
    yield from _guarded(item)


def pytest_sessionfinish(session: pytest.Session) -> None:
    if session.config.stash[_guard_key].violations and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    violations = config.stash[_guard_key].violations
    if violations:
        terminalreporter.section("photomigrate safety guard", red=True)
        for violation in violations:
            terminalreporter.line(f"blocked {violation.event} on {violation.path!r}")


def _describe(violations: list[Violation]) -> str:
    return "; ".join(f"{v.event} {v.path!r}" for v in violations)
