# Design notes

Takeout structure and quirks: see [TAKEOUT_FORMAT.md](TAKEOUT_FORMAT.md).

## Ingest (M1-04)

`photomigrate ingest <takeout-dir> [--apply]` catalogues every Google Photos item **straight
from the zip parts**: no extraction, and nothing is ever written to the zips. The real export
is about 600 GB (12 × 50 GB) on the MacBook Air's external drive.

- `ingest.takeout`: lists the parts, or an already unzipped `Takeout/` tree. Only
  `Takeout/Google Photos/` is used; other files are counted and ignored. It rejects unsafe
  member paths (zip slip) and parts that disagree about the same file. `local_copy()` streams
  one entry into a scratch file and hashes it on the way. The original's location is
  `<part>::<member>`, relative to the Takeout folder, so the catalogue can move between
  machines.
- `ingest.names` and `ingest.sidecars`: parse names (`(N)` index, edit suffixes including ones
  cut by the 47-character limit, dates in names) and match media to sidecars folder by folder.
  Every candidate pair gets a score: exact 3, cut-but-a-prefix 2, legacy without the media
  extension 1. Pairs are assigned best first, each sidecar to one original at most. Edited
  copies take their original's sidecar: the original with the same stem and index, preferring
  the exact extension, then the same extension ignoring case. Two originals that differ only in
  extension case, plus an edit with its case flipped, cannot be told apart, and the matcher then
  picks the exact-extension one. Every media file gets a `Match`. Unclaimed sidecars become
  orphans.
- `ingest.exif`: a typed PyExifTool wrapper. Every path is announced to the safety guard first.
  `write()` always creates a *new* file (`-o`). Photos get EXIF `DateTimeOriginal` and
  `OffsetTimeOriginal`, GPS, and `XMP-dc:Description`. GIFs get the XMP equivalents. Videos get
  `QuickTime:CreateDate` and `Keys:CreationDate`, `Keys:GPSCoordinates` and `Keys:Description`.
- `ingest.policy`: the file's own date wins. A plausible sidecar date fills a gap, written in the
  home time zone (`--timezone`, by default the Mac's) and flagged `offset-assumed`. Placeholder
  dates, dates after the upload, and the upload time used as a capture date are never written,
  only flagged. Disagreements become `date-conflict`. GPS and caption come from the sidecar when
  missing or different. Every date candidate is recorded (exif or quicktime, sidecar-taken,
  sidecar-created, filename, mtime).
- `ingest.stage`: one `ingest` action per media file, plus `flag-orphan-sidecar` and
  `flag-unknown-file`. Apply hashes the item, reads its metadata, and stores the item, its flags
  (match method, edited, google-creation, policy flags) and its **metadata plan** (catalogue v2
  column `metadata_plan`). Working copies are written at import time (M4-01) for approved items.

### Albums, copies, favourites, captions, people (M1-05)

- **An album is a folder with a `metadata.json`** (the name is localised). That rule doesn't
  depend on the export language. Year folders have no such file. A broken `metadata.json`
  falls back to the folder name.
- **Plain folders are ingested first.** So an item's main record is its `Photos from YYYY` file
  when one exists. Items that live only in albums, like Delmar's shared album, still get one.
- **A later file with the same SHA-256 and the same Google Photos URL** (from its sidecar) is the
  *same item*. It's stored in `item_copies`, and its folder adds an `album_members` row. Same
  bytes but a different URL means a separate Google item, flagged
  `same-bytes-as-another-item`. `catalogue.item_for_key()` maps any Takeout path to its one item,
  which is the basis for reconciliation (M4-03).
- **Favourites and captions** are on `items` (`favorite`, `description`). Face-tag names become
  `People/<name>` keywords (`keywords` table, replaced as a set per prefix). Catalogue v3 adds
  `albums`, `album_members`, `item_copies`, `keywords` and `items.google_url`.

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

Absolute paths are checked as written and with symlinks resolved. A relative path is judged
only by whether the cwd itself is protected. `shutil.rmtree` and `os.fwalk` emit bare names
relative to a directory fd, which was itself checked when it was opened, so joining those
names to the cwd gives false hits (a tmp `data-in` read as `<repo>/data-in`). The known gap
is cwd-relative access into a protected root from its parent, so tests use absolute paths.

The pytest plugin also forbids `<rootdir>/data-in/`, where Delmar's real Takeout and Timeline
samples live.

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
