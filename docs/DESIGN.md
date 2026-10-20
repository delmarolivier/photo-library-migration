# Design notes

Takeout structure and quirks: see [TAKEOUT_FORMAT.md](TAKEOUT_FORMAT.md).

## Safety rails (M1-01)

### Media guard (`photomigrate.safety.PathGuard`, CLAUDE.md rule 4)

A PEP 578 audit hook installed once per process. While a guard is active it inspects
filesystem audit events (`open`, `os.*`, `shutil.*`, `glob.*`, `pathlib.*`, `tempfile.*`,
`sqlite3.connect`) and process launches (`subprocess.Popen`, `os.system`, `os.exec`,
`os.posix_spawn`, `os.spawn`, with argv tokens and `--opt=path` values checked). A protected
path raises `ForbiddenPathError` *before* the operating system sees the call, and the
violation is recorded.

Protected (`GuardRules`):

- always: `~/Pictures` for both the passwd home and `$HOME`, plus `PHOTOMIGRATE_GUARD_FORBID`
  entries;
- unless under an allowed root (tmp, `--basetemp`, `tests/fixtures/`): any path component
  matching `*.photoslibrary`, `Takeout`, `takeout-*.zip` or `takeout-*.tgz` (case-insensitive).

Absolute paths are checked as written and with symlinks resolved. Relative paths are judged
by the cwd plus the always-forbidden roots, because `shutil.rmtree` and `os.fwalk` emit names
relative to a directory fd that was itself checked when it was opened.

The pytest plugin `photomigrate.testing.pytest_guard` (loaded from `tests/conftest.py`)
activates a guard for the session. It fails the test phase (setup, call or teardown) that
caused a violation, even when the code swallowed the exception, and it fails the session for
violations that happen outside any test.

**Rule for later stages:** paths handed to an external tool without appearing in its argv
(PyExifTool's stay-open mode writes them to stdin) must first go through
`photomigrate.safety.announce_path(path)`, so the guard sees them.

### No-delete check (`photomigrate-no-delete-check`, CLAUDE.md rule 3)

An AST scan of `src/` that flags calls to, and imports of, names whose words include
`delete`, `trash`, `unlink`, `rmtree`, `rmdir`, `removedirs` or `send2trash`, plus `remove`
followed by asset/photo/media/item/file, plus `os.remove`. The phrase "to delete" (the
"To Delete" album) is allowed. Strings are not scanned, so SQL `DELETE FROM` on catalogue
rows is fine.

Known gaps for M4-02 to close: AppleScript/JXA `delete` inside strings passed to `osascript`,
dynamic access (`getattr(os, "remove")`), and moves or renames of source files.
