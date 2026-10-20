# Design notes

Takeout structure and quirks: see [TAKEOUT_FORMAT.md](TAKEOUT_FORMAT.md).

## Catalogue and stage runner (M1-03)

### Catalogue (`photomigrate.catalogue`)

SQLite at `data/catalogue.sqlite` (gitignored). The schema version is `PRAGMA user_version`.
`MIGRATIONS` is an ordered tuple numbered 1, 2, 3…, and each migration runs in a single
transaction that also bumps the version. A catalogue newer than the code is refused.
Add a migration for any schema change, and never edit a shipped one.

Tables (v1):

| Table | Holds |
|---|---|
| `items` | one row per Google or iCloud item, identified by `(source, source_key)` (the Takeout path or Photos UUID): paths, sha256, perceptual hashes, size, duration, GPS, title, description, favourite, flags (a JSON array) |
| `date_candidates` | one value per `(item, source)`. Sources: `exif`, `quicktime`, `sidecar-taken`, `sidecar-created`, `filename`, `mtime`, `icloud`, `user`. Naive ISO values mean local time with an unknown offset. |
| `groups`, `group_members` | `duplicate`, `best-shot`, `date-batch`, `live-pair` groups, with a member role and rank |
| `decisions` | the user's answers per item or group and kind (import, keep, date…) |
| `stage_runs`, `actions` | the action log: every applied run and each action's key, status and error |

Writes are idempotent. Upserts change a row, and its `updated_at`, only when a value actually
differs.

### Runner (`photomigrate.runner`)

A stage implements `plan(catalogue) -> Iterable[Action]`, which writes nothing, and
`apply(action, catalogue)`, which must be idempotent. `run_stage`:

- **dry run** (the default): plans against `Catalogue.snapshot()`, an in-memory copy (the file is
  opened read-only, or the catalogue starts empty if there is no file), then saves the plan to
  `data/plans/<stage>-<time>-dry-run.json`. That plan file is the only side effect.
- **`--apply`**: skips actions already applied by earlier runs (matched by `Action.key`, a hash
  of kind, target and details, so a changed input yields a new key). It applies each remaining
  action in its own transaction together with its log entry, and stops at the first failure.
  The next run resumes from the failed action. When nothing is pending, nothing is written,
  not even a run record.

`render_result` prints a summary table, the first 50 actions and the next step.


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
