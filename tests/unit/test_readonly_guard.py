"""ReadOnlyGuard: reads inside a protected tree pass, any write is refused (CLAUDE.md rule 6)."""

import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from photomigrate.safety import ForbiddenPathError, ReadOnlyGuard, announce_write

LIB = Path("/photomigrate-fake/Library.photoslibrary")
DB = LIB / "database" / "Photos.sqlite"
OUTSIDE = Path("/photomigrate-fake/elsewhere/x.jpg")


@pytest.fixture
def guard() -> ReadOnlyGuard:
    return ReadOnlyGuard(LIB)


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("open", (str(DB), "r", os.O_RDONLY)),
        ("open", (str(DB), "rb", os.O_RDONLY)),
        ("open", (str(DB), None, os.O_RDONLY)),
        ("sqlite3.connect", (f"{DB.as_uri()}?mode=ro",)),
        ("shutil.copyfile", (str(DB), str(OUTSIDE))),
        ("os.listdir", (str(LIB),)),
        ("open", (str(OUTSIDE), "w", os.O_WRONLY | os.O_CREAT)),
        ("os.remove", (str(OUTSIDE), None)),
        ("open", (3, "w", os.O_WRONLY)),
    ],
)
def test_reads_and_writes_elsewhere_pass(
    guard: ReadOnlyGuard, event: str, args: tuple[Any, ...]
) -> None:
    guard.check(event, args)

    assert guard.violations == []


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("open", (str(DB), "w", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)),
        ("open", (str(DB), "r+", os.O_RDWR)),
        ("open", (str(DB), "ab", os.O_WRONLY | os.O_APPEND | os.O_CREAT)),
        ("open", (str(LIB / "new.txt"), "x", os.O_WRONLY | os.O_CREAT | os.O_EXCL)),
        ("open", (str(DB), None, os.O_RDWR)),
        ("sqlite3.connect", (str(DB),)),
        ("sqlite3.connect", (f"{DB.as_uri()}?mode=rw",)),
        ("os.rename", (str(OUTSIDE), str(LIB / "x.jpg"), None, None)),
        ("os.rename", (str(LIB / "x.jpg"), str(OUTSIDE), None, None)),
        ("os.remove", (str(LIB / "x.jpg"), None)),
        ("os.mkdir", (str(LIB / "new"), 0o777, None)),
        ("os.rmdir", (str(LIB / "originals"), None)),
        ("os.utime", (str(DB), None, None, None)),
        ("os.chmod", (str(DB), 0o644, None)),
        ("os.truncate", (str(DB), 0)),
        ("shutil.copyfile", (str(OUTSIDE), str(LIB / "x.jpg"))),
        ("shutil.rmtree", (str(LIB), None)),
        ("photomigrate.write", (str(LIB / "x.jpg"),)),
    ],
)
def test_writes_inside_the_tree_are_refused(
    guard: ReadOnlyGuard, event: str, args: tuple[Any, ...]
) -> None:
    with pytest.raises(ForbiddenPathError, match="read-only"):
        guard.check(event, args)

    assert [v.event for v in guard.violations] == [event]


def test_active_read_only_guard_blocks_real_writes_but_not_reads(tmp_path: Path) -> None:
    library = tmp_path / "Test.photoslibrary"
    (library / "database").mkdir(parents=True)
    db = library / "database" / "Photos.sqlite"
    sqlite3.connect(db).execute("CREATE TABLE t (x)").connection.commit()
    original = db.read_bytes()
    guard = ReadOnlyGuard(library)

    with guard:
        assert db.read_bytes() == original
        sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True).execute("SELECT * FROM t").fetchall()
        with pytest.raises(ForbiddenPathError):
            db.write_bytes(b"oops")
        with pytest.raises(ForbiddenPathError):
            sqlite3.connect(db)
        with pytest.raises(ForbiddenPathError):
            shutil.copyfile(tmp_path / "missing.txt", library / "x.txt")
        with pytest.raises(ForbiddenPathError):
            announce_write(library / "x.jpg")

    assert db.read_bytes() == original
    assert len(guard.violations) == 4
    db.write_bytes(original)  # the guard is off again


def test_relative_paths_inside_the_tree_are_judged_by_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "Test.photoslibrary"
    library.mkdir()
    guard = ReadOnlyGuard(library)
    monkeypatch.chdir(library)

    with pytest.raises(ForbiddenPathError):
        guard.check("open", ("x.txt", "w", os.O_WRONLY | os.O_CREAT))
    monkeypatch.chdir(tmp_path)
    guard.check("open", ("x.txt", "w", os.O_WRONLY | os.O_CREAT))
