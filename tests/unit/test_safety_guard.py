"""PathGuard turns PEP 578 audit events into hard failures on protected paths."""

import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from photomigrate import safety
from photomigrate.safety import (
    ForbiddenPathError,
    GuardRules,
    PathGuard,
    Violation,
    announce_path,
)

FAKE = Path("/photomigrate-fake")
PICTURES = FAKE / "home" / "Pictures"
SCRATCH = FAKE / "tmp"
PHOTO = PICTURES / "IMG_0001.jpg"


@pytest.fixture
def guard() -> PathGuard:
    return PathGuard(GuardRules(forbidden_roots=(PICTURES,), allowed_roots=(SCRATCH,)))


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("open", (str(PHOTO), "r", 0)),
        ("open", (PHOTO, "rb", 0)),
        ("os.listdir", (str(PICTURES),)),
        ("os.scandir", (os.fsencode(PICTURES),)),
        ("os.rename", (str(SCRATCH / "a.jpg"), str(PHOTO), None, None)),
        ("shutil.copyfile", (str(PHOTO), str(SCRATCH / "copy.jpg"))),
        ("glob.glob", (f"{PICTURES}/*.jpg", False)),
        (
            "sqlite3.connect",
            (f"file:{PICTURES}/Photos Library.photoslibrary/Photos.sqlite?mode=ro",),
        ),
        ("subprocess.Popen", ("exiftool", ["exiftool", "-json", str(PHOTO)], None, None)),
        ("subprocess.Popen", (None, [b"exiftool", os.fsencode(PHOTO)], None, None)),
        ("subprocess.Popen", (None, ["exiftool", PHOTO], None, None)),
        ("subprocess.Popen", (None, f"exiftool -json '{PHOTO}'", None, None)),
        ("subprocess.Popen", (None, ["exiftool", f"-o={PHOTO}"], None, None)),
        ("subprocess.Popen", (None, ["ls"], str(PICTURES), None)),
        ("subprocess.Popen", (str(PICTURES / "evil"), ["evil"], None, None)),
        ("os.system", (f"ls '{PICTURES}'",)),
        ("os.exec", ("/bin/ls", ["/bin/ls", str(PICTURES)], {})),
        ("os.posix_spawn", ("/bin/ls", ("/bin/ls", str(PICTURES)), {})),
        ("os.spawn", (0, "/bin/ls", ["/bin/ls", str(PICTURES)], {})),
        ("photomigrate.path", (str(PHOTO),)),
    ],
)
def test_protected_path_in_event_is_blocked_and_recorded(
    guard: PathGuard, event: str, args: tuple[Any, ...]
) -> None:
    with pytest.raises(ForbiddenPathError, match=re.escape("CLAUDE.md rule 4")):
        guard.check(event, args)

    assert len(guard.violations) == 1
    assert guard.violations[0].event == event


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("open", (3, "r", 0)),
        ("open", (str(SCRATCH / "IMG_0001.jpg"), "r", 0)),
        ("sqlite3.connect", (":memory:",)),
        ("subprocess.Popen", ("exiftool", ["exiftool", "-ver"], None, {"X": str(PICTURES)})),
        ("subprocess.Popen", (None, "echo 'unbalanced", None, None)),
        ("subprocess.Popen", (None, 42, None, None)),
        ("os.putenv", ("PHOTO", str(PHOTO))),
        ("socket.connect", (object(), ("127.0.0.1", 80))),
        ("os.chmod", (str(SCRATCH / "x"), 0o644, None)),
    ],
)
def test_unprotected_or_irrelevant_events_pass(
    guard: PathGuard, event: str, args: tuple[Any, ...]
) -> None:
    guard.check(event, args)

    assert guard.violations == []


def test_tilde_tokens_in_shell_commands_are_expanded(
    guard: PathGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(FAKE / "home"))

    with pytest.raises(ForbiddenPathError):
        guard.check("os.system", ("ls ~/Pictures",))


def test_violation_records_event_and_path(guard: PathGuard) -> None:
    with pytest.raises(ForbiddenPathError, match=re.escape("IMG_0001.jpg")):
        guard.check("open", (str(PHOTO), "r", 0))

    assert guard.violations == [Violation(event="open", path=str(PHOTO))]


def test_active_guard_blocks_a_real_open(tmp_path: Path) -> None:
    pictures = tmp_path / "home" / "Pictures"
    pictures.mkdir(parents=True)
    photo = pictures / "IMG_0001.jpg"
    photo.write_bytes(b"not really a jpeg")
    guard = PathGuard(GuardRules(forbidden_roots=(pictures,), allowed_roots=()))

    guard.activate()
    try:
        with pytest.raises(ForbiddenPathError):
            photo.read_bytes()
    finally:
        guard.deactivate()

    assert photo.read_bytes() == b"not really a jpeg"
    assert [v.path for v in guard.violations] == [str(photo)]


def test_innermost_active_guard_handles_events() -> None:
    outer = PathGuard(GuardRules(forbidden_roots=(PICTURES,), allowed_roots=()))
    inner = PathGuard(GuardRules(forbidden_roots=(PICTURES,), allowed_roots=()))

    outer.activate()
    inner.activate()
    try:
        with pytest.raises(ForbiddenPathError):
            sys.audit("photomigrate.path", str(PHOTO))
        inner.deactivate()
        with pytest.raises(ForbiddenPathError):
            sys.audit("photomigrate.path", str(PHOTO))
    finally:
        inner.deactivate()
        outer.deactivate()

    assert len(inner.violations) == 1
    assert len(outer.violations) == 1


def test_announce_path_raises_the_audit_event_for_piped_paths() -> None:
    guard = PathGuard(GuardRules(forbidden_roots=(PICTURES,), allowed_roots=()))

    guard.activate()
    try:
        announce_path(SCRATCH / "fine.jpg")
        with pytest.raises(ForbiddenPathError):
            announce_path(PHOTO)
    finally:
        guard.deactivate()

    assert guard.violations == [Violation(event="photomigrate.path", path=str(PHOTO))]


def test_dispatcher_ignores_events_when_no_guard_is_active(monkeypatch: pytest.MonkeyPatch) -> None:
    # CPython pauses tracing inside audit hooks, so coverage only sees the dispatcher
    # when it is called directly.
    monkeypatch.setattr(safety, "_active_guards", [])

    safety._audit("photomigrate.path", (str(PHOTO),))


def test_dispatcher_routes_events_to_the_innermost_guard(
    guard: PathGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(safety, "_active_guards", [guard])

    with pytest.raises(ForbiddenPathError):
        safety._audit("photomigrate.path", (str(PHOTO),))
