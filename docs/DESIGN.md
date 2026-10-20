# Design notes

Takeout structure and quirks: see [TAKEOUT_FORMAT.md](TAKEOUT_FORMAT.md).

## Quality flags (M3-01)

Ingest and index measure every item once, while the file is at hand (`photomigrate.quality`,
stored as JSON in `items.quality`, catalogue v9). Then `photomigrate analyse` runs `quality`
(`analyse.quality`), which only compares the measurements with the thresholds, so trying new
thresholds is quick.

- **Photos** are decoded once for both their hashes and their metrics (`hashing.open_picture`:
  upright, long side at most 1024 px; JPEGs are decoded at reduced size).
  - `sharpness`: the variance of the Laplacian. Low means blurry.
  - `brightness`: the mean luminance. `dark_fraction` and `bright_fraction`: the share of
    pixels below 16 and above 240.
  - `contrast`: the luminance standard deviation. Near 0 is a flat frame.
  - `megapixels` and `short_side`, from the full upright size.
- **Videos** reuse the 8 frames sampled for the signature (one ffmpeg pass): mean
  `brightness`, and `motion`, the mean change between consecutive samples.
- **Flags**, with the default thresholds (`photomigrate.example.toml`):

  | Flag | When |
  |---|---|
  | `blurry` | sharpness < 30 |
  | `low-resolution` | < 0.3 MP |
  | `under-exposed` | brightness < 40, or more than half the pixels near black |
  | `over-exposed` | brightness > 215, or more than half the pixels near white |
  | `accidental` | contrast < 10 (pocket shot, black frame, lens cap) |
  | `short-clip` | < 1.5 s, never a Live Photo motion half (paired or `unpaired-motion`) |
  | `black-video` | brightness < 12 |
  | `static-video` | motion < 2 |
  | `tiny-video` | short side < 240 px |

- **Scores:** each flag has a score from 0 (just past the threshold) to 1 (as bad as it gets),
  in the `quality_flags` table. A re-run with other thresholds replaces an item's flags.
  Nothing is decided: the review gallery shows the flags, and best-shot (M3-02) can use them.
- **Config:** `--config FILE` (TOML). Unknown sections or settings, wrong types and negative
  values are errors, so a typo never silently falls back to a default.
- **On the real sample** (3,282 photos, 26 videos):
  - Sharpness spans 15 to 6,000 (median 170). The usual default of 100 would flag a quarter of
    the photos; 30 flags 29 (0.9%).
  - Nothing else is flagged. Mean brightness stays between 44 and 175, contrast is at least 30,
    the smallest photo is 640x480, and every video is 6.6 s or longer, with plenty of motion.
  - Decoding once for hashes and metrics left dedupe unchanged: the same 258 duplicate groups
    and 1,562 edit pairs, member for member.

## Edited versions (M2-04)

`photomigrate analyse` first runs `edited-versions` (`analyse.edited`). It links every Google
item flagged `edited` (localised and cut suffixes included) to its original in the same folder.
The rule is the sidecar matcher's `original_of`: same stem and `(N)`, exact extension first.
Album copies resolve to their one item.

- **Storage:** each link is an `edit-pair` group (catalogue v8 rebuilds `groups` to allow the
  new kind; migrations run with foreign keys off, then checked). Members are `original`
  (rank 0) and `edited` (rank 1). Details hold the `policy` and `keep`.
- **Policies** (`--edited-policy`):
  - `pair`, the default: keep both;
  - `original-only`;
  - `edited-only`.
  Switching the policy updates every group. The importer reads `keep`.
- **Orphans:** an edit without its original is flagged `unpaired-edit`. Originals get `has-edit`.
- **Dedupe:** edits never take part, so an edit is never proposed as a duplicate of its original.
- **On the real sample:** 1,562 edit pairs and no orphans. Photo dedupe found 256 groups in that
  one shared album, 200 of them `IMG_1234.JPG` + its re-upload `IMG_1234_2.jpg`. The members are
  visually identical (256-bit distance 0–3) and share the EXIF second.

## Video dedupe (M2-03)

`photomigrate analyse` also runs `dedupe-videos` (`analyse.dedupe_video`).

- **Signatures:** ingest and index store a signature per video (catalogue v7
  `video_signature`, `bitrate`): pHashes of 8 frames sampled evenly by one ffmpeg pass (64x64
  greyscale). The bitrate is size × 8 / duration.
- **Matching:** same SHA-256 (`exact`), or a `near` match. A near match needs:
  - durations within ±0.5 s (only that window is compared);
  - a mean aligned-frame distance of 8 bits or less;
  - an aspect ratio within 2%;
  - the same moment, if both carry a QuickTime date.
  In tests, re-encoded or downscaled copies come out ≤ 4 apart, and different clips of the same
  length > 12 apart.
- **Exclusions:** Live Photo motion clips (`live-photo-motion`, `unpaired-motion`).
- **Keeper order:** resolution, then bitrate, then metadata, then iCloud, then the key.
- **Shared plumbing:** groups go through `analyse.grouping`. Each stage owns the `duplicate`
  groups with its key prefix (`dup-` photos, `vdup-` videos) and retires only its own stale ones.

## Photo dedupe (M2-02)

`photomigrate analyse [--apply]` runs `dedupe-photos` (`analyse.dedupe_photo`) over every
catalogued photo, Google and iCloud. Excluded: Google `edited` copies (M2-04 links them) and
`google-creation`s (clutter queue). Thresholds were measured on generated scenes:

| Pair | 64-bit pHash | 256-bit pHash (`phash_fine`) |
|---|---|---|
| resized / recompressed (q25) / HEIC↔JPEG copy | ≤ 4 | ≤ 8 |
| 95% crop | from 2 | ≥ 18 |
| 90% crop | from 2 | ≥ 54 |
| the same scene a moment later (shapes moved 1%) | from 0 | from 8 |

Rules, all configurable (`DedupeConfig`, CLI flags):
- **Edges:** same SHA-256 (`exact`), or same Live Photo `content_id` (`content-id`), or a
  `near` match. A near match needs:
  - 64-bit pHash ≤ 8 (candidate search: numpy brute force with `bitwise_count`, about 50 s per
    200k photos);
  - then 256-bit pHash ≤ 12, or ≤ 6 when either photo lacks an EXIF time (`fine_max_untimed`);
  - dHash ≤ 10;
  - aspect ratio within 2% (rotation ignored);
  - if both carry an EXIF time, **the exact same second**. Mixed local-vs-UTC comparisons
    allow a whole time-zone offset.
- **Groups are stars around the keeper (`grouping.star_groups`):** every member must match the
  keeper *directly*, so chains (A~B~C with A ≠ C) can't pull different pictures together.
  `method` says how the members match the keeper.
- **Keeper order:** resolution, then metadata (date, GPS, caption), then iCloud, then the key.
- **Stored as proposals only:** `groups(kind='duplicate')` with member roles `keeper` /
  `duplicate`, plus `motion` for the clip of a Live Photo still. Stale groups are retired. No
  `decisions` are written; review (M3-06) confirms.

**Validated on the real sample with pixels** (2026-10-20). Each proposed pair was decoded,
orientation-corrected and compared by SSIM/PSNR; the measurement scripts live outside the repo.
- **The first rules** (1 s tolerance, transitive groups): 270 of 287 pairs were the same image,
  and 17 were different shots. 13 of those were 1 s apart, 3 were chained, and 1 lacked EXIF.
- **Measurements behind the fixes:**
  - all 244 true duplicate pairs with EXIF on both sides shared the exact second;
  - true duplicates without EXIF were within 2 bits on the 256-bit hash;
  - the different shot was 10 bits away.
- **After the fixes:** 276 of 276 pairs are the same image (254 at full size, 22 smaller re-saves
  with SSIM@128 ≥ 0.983).
- **Recall:** of 2,258 loosely similar pairs left ungrouped, 2,256 are different pictures. The
  other 2 share the EXIF second but are 14 bits apart: likely burst frames.
- **Control:** each keeper against its nearest-in-time photo outside the group gave 256 of 257
  "different".

## iCloud index (M2-01)

`photomigrate index <Library.photoslibrary> [--apply]` (`index.icloud`, macOS only, osxphotos).

**Nothing in the real library is written, not even SQLite bookkeeping.** A WAL-mode SQLite
reader updates the database's `-shm` file even with `mode=ro`, and tests caught osxphotos
changing its mtime. So osxphotos reads a **shadow library**: a temporary folder holding a
private clone of `database/` (an APFS clone via `cp -c`, else a copy) and read-only symlinks to
`originals/`, `resources/` and the rest. Paths come back through the symlinks and are mapped to
the real library. A `safety.ReadOnlyGuard` on the real library is active for the whole run.
It refuses write-mode opens, read-write sqlite connections, renames, removals, mkdir, chmod,
utime, copies *into* the library, hard links, symlinks created inside it, and exiftool writes
(`announce_write`).

Per asset (catalogue v5 adds `scores` and `timezone`):
- the Photos uuid, original filename, the original's path relative to the library;
- SHA-256 and pHash/dHash (`hashing`, orientation-aware) of the original;
- the Photos date with its offset (date candidate `icloud`) and embedded EXIF/QuickTime dates;
- GPS, size, video duration and Live Photo content id (from exiftool);
- favourite, caption, albums (`albums`, source `icloud`), keywords, and Apple's 27 scores;
- flags: `edited`, `live-photo`, `hidden`, `burst`, `shared`, `cloud-asset`.

Originals that aren't on this Mac become `index-missing` actions: the item is flagged
`original-missing`, has no hashes, and is listed by the CLI. Quit Photos while indexing, so the
cloned database is a consistent snapshot.

Tests (`tests/macos`, marked `macos`) use osxphotos' own test libraries (Test-27.0_DevBeta and
Test-Live-15.7.2), fetched once at a pinned commit into the system temp dir. Each test gets
its own APFS clone. They prove the library is byte- and mtime-identical after a dry run and an
apply, and that the guard saw zero writes.

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

### Live Photos (M1-06)

`photomigrate ingest` runs a second stage, `live-photos`, over the catalogue
(`ingest.livephotos`):

- **Stills** are HEIC/HEIF/JPEG files that are neither edits nor Google creations. **Motion
  candidates** are MOV/MP4 clips of 3.5 s or less.
- **Pairing order:** Apple's `ContentIdentifier` first (catalogue v4 `items.content_id`).
  Exiftool reads it from the still's Apple MakerNotes or from the clip's `Keys:ContentIdentifier`.
  Then the same folder and name stem (including `(N)`) with capture times at most 3 s apart
  (`dates.moment_gap`, which allows for offset-less EXIF). If a time is unknown, the name
  alone decides. Closest pairs win, and each file pairs once.
- **Storage:** pairs become `live-pair` groups (key = the still's path, members still/motion),
  and the items are flagged `live-photo-still` / `live-photo-motion`.
- **Unpaired candidates** are clips that carry an identifier or have a same-named still, but no
  partner. They're flagged `unpaired-motion` for review. Other short clips are ordinary videos.
- **Fixture limit:** fixtures embed the identifier only in the clip. exiftool can't create
  Apple MakerNotes, so the still's copy can't be faked.

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
- **Undone actions:** an applied action is done only if no action on the same target that the
  stage no longer plans was applied after it. So a setting changed and then changed back
  (`--edited-policy pair`, `original-only`, `pair`) re-applies the first action instead of
  leaving the middle setting in place.

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
