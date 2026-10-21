# Progress log

## Current state
- Milestone: 3. Quality and review
- Last completed issue: [M4-03] CLA-91 (reconciliation report). M4-04 (CLA-92): the runbook
  is written (docs/RUNBOOK.md). Every issue that needs no human action is done.
- Next: Delmar reviews the runbook, then runs the rehearsal with Claude (CLA-92).
- Coverage: 100% line + branch (gate 90%)
- Open questions for Delmar:
  - The rehearsal (CLA-92) once the drive is there: it imports the one-album sample into the
    real library on the Air, with a GO before each `--apply`. Delmar reviewed the runbook on
    2026-10-21.
  - The rehearsal (CLA-92) once the drive is there: it imports the one-album sample into the
    real library on the Air, with a GO before each `--apply`. Delmar reviewed the runbook on
    2026-10-21.
  - Needs you: run the real Photos.app import test once on the Air. Create an empty library
    under /tmp in Photos (hold Option at launch), open it, and allow Automation. Then run
    `PHOTOMIGRATE_PHOTOS_APP_LIBRARY=/tmp/… uv run pytest tests/macos/test_photos_app_import.py -m macos --no-cov`
    in Terminal (with Full Disk Access) at the Air.
  - The external APFS drive (2 TB or more, 4 TB to be comfortable) for the MacBook Air: Delmar
    is getting one (2026-10-21). Then download the 12 Takeout parts onto it before the links
    expire (about 2026-10-27, a week after the export).
  - Before the first real `index` (CLAUDE.md rule 10):
    - move the Photos library onto the external drive and turn on "Download Originals";
    - give `sshd-keygen-wrapper` Full Disk Access (System Settings > Privacy & Security);
    - quit Photos while indexing;
    - give the go-ahead.

## Session log
### 2026-10-21: session 2 (continued): the real Photos.app test
- Both opt-in Photos.app tests pass on the Air with a throwaway library in /tmp: the fixture
  import (albums, favourite, caption, keywords), and a genuine iPhone Live Photo, which Photos
  joins into one asset.
- Fixed on the way:
  - the test guard allows /tmp;
  - an unreadable-settings error now names the missing Full Disk Access;
  - an unjoined Live Photo clip is recorded as its own asset.
- The Air: Terminal and sshd-keygen-wrapper have Full Disk Access; tmux is installed.
  Photos-driving commands run in a tmux session started in Terminal at the Air (macOS never
  offers Automation to ssh sessions). RUNBOOK says so.

### 2026-10-21: session 2: Delmar's answers and the runbook review
- Delmar's answers:
  - best-shot stays at 0.94 until he judges the groups in the rehearsal;
  - location backfill borrows across devices (`any_device = true`);
  - he is getting the drive;
  - a one-off copy of the library is the backup (no Time Machine);
  - permissions are taken back after the migration;
  - one catalogue for the rehearsal and the full run, all on the drive;
  - two extra checks in Photos: the asset count, and the iPhone sync.
- Code:
  - `confirm-backup` asks for a full copy of the library;
  - `Catalogue.own_imports()`: a re-index after an import keeps the importer's own assets
    out of every analysis stage and out of To Delete;
  - `report` gives the imported asset count, to compare with "From Google" in Photos.
- Runbook rewritten accordingly (`PHOTOMIGRATE_*` variables, `$W`, `"$L"`).
- Tests: 1,163, 100%.

### 2026-10-21: session 1 (continued): M4-04 (runbook)
- docs/RUNBOOK.md: preparation, backup, permissions, rehearsal on the sample (its own
  catalogue), Delmar's checks in Photos, the full run in batches, the final report, and the
  manual cleanup. There is a GO checkpoint before every `--apply`.
- Dry rehearsal of the commands on the sample here (no library): it showed that `$R`
  expansion fails in zsh (the runbook now uses the `PHOTOMIGRATE_*` variables), and that
  best-shot pairs counted as pending although nobody is asked about them (fixed, tested).
- Tests: 1,159, 100%.

### 2026-10-21: session 1 (continued): M4-03
- `photomigrate report`: HTML and CSV, one status per Google item (imported with uuid,
  duplicate-of-existing with the uuid that stays, rejected-by-user with the reason, or
  unaccounted with why), totals and bytes (catalogue v16 `items.size`), the SHA-256 trail,
  date and location fixes, To Delete count; exit 1 while anything is unaccounted.
- Fix found by the report: edits of Google creations follow their original's clutter
  answer; unpaired edits are queued themselves.
- Tests: 1,158, 100%. The full-pipeline test ends with a clean report.

### 2026-10-21: session 1 (continued): M4-02
- `import` stages and unstages "To Delete" so the album is exactly the rejected iCloud items
  (catalogue v15 `staged`); `PhotosLibrary.remove_from_album` (album membership only).
- `safety.NoDeleteGuard`, used by `import --apply` on the library (the Takeout stays under
  `ReadOnlyGuard`). The static check now catches `getattr` lookups, Photos delete scripts,
  and moves/renames (allowed only with a reason comment).
- A full-pipeline test with every delete API rigged to fail on media.
- Tests: 1,138, 100%.

### 2026-10-20: session 1 (continued): M4-01
- `importer.outcomes`: every Google item is import, duplicate-of-existing, rejected-by-user,
  follows (Live clip) or pending (an unanswered question); `to_delete` lists the rejected
  iCloud items.
- `importer.library`: `PhotosLibrary` protocol; `PhotoscriptLibrary` refuses unless Photos
  has the given library open. `testing.photos.DisposableLibrary` for tests.
- `importer.stage` + `photomigrate import` and `confirm-backup`: batches of working copies
  (exiftool `-o`) into "From Google", with albums, favourites, captions and keywords; dates
  and places for existing iCloud items; catalogue v14 `imports`. Config `[import]
  batch_gigabytes`; the config file can now name a section differently from its field.
- Tests: 1,109, 100%. The real Photos.app test is opt-in (see the open questions).

### 2026-10-20: session 1 (continued): M3-06
- `photomigrate review` (FastAPI, Jinja2, htmx 2.0.4 vendored and checked against its
  published SHA-384): eight queues; decisions are checked, then saved at once; bulk answers
  for clutter categories and edit-pair policies; cached thumbnails from the zips and the
  library (HEIC, video posters); keyboard shortcuts; progress bars.
- Bound to 127.0.0.1 only (tested on a real socket, and seen live with lsof). Other host
  names are refused, and writes need a same-origin Origin header.
- Real sample (a /tmp copy): pages render in 10 to 90 ms; thumbnails take 68 ms from the
  4 GB zip.
- Tests: 1,062, 100%.
- Handoff for M4-01: decisions to read are listed in DESIGN, "Review gallery". Also
  `date_fixes.planned_write` (dates), `locations.approved_location` (GPS plus the keyword
  `location-inferred`), and the clutter, quality and unmatched rejects.

### 2026-10-20: session 1 (continued): M3-05
- `photomigrate.timeline` parses the iOS Timeline export (visits, activities, paths) and
  locates a moment; `testing.timeline.TimelineBuilder` writes synthetic exports.
- `analyse.locations` proposes places for shots without GPS from the same camera's located
  shots within 30 minutes (interpolated between two), else from `--timeline`. Proposals
  (catalogue v13 `inferred_locations`) are written only once approved (`approved_location`,
  keyword `location-inferred`). `any_device` switch; config accepts `true`/`false`.
- Real sample: nothing to propose with the same device (iPhone 4S shots have no GPS at all);
  56 across devices. Question posted on CLA-87.
- Tests: 1,000, 100%.

### 2026-10-20: session 1 (continued): M3-04
- Ingest and index detect text with Vision (`text_regions`, catalogue v12 `items.text`); index
  records Photos' `screenshot` flag. open_clip text prompts (`embed_texts`) give zero-shot
  labels where Vision measured no text.
- `analyse.clutter` scores google-creation, screenshot, document, meme and forward per item
  (evidence combined as independent clues, camera penalties), and queues the best category
  at or above `[clutter] min_score` (0.5) in the `clutter` table with its evidence.
- Real sample: the first rules queued 86 "documents" from Vision boxing textured scenes and 32
  "screenshots" that were mostly 1080p videos. After the fixes: about 138 creations,
  7 documents, 7 screenshots.
- Tests: 956, 100%.
- Handoff: M3-06 shows the queue by category with the evidence and records decisions per
  item; Google creations are never imported unless Delmar picks them there.

### 2026-10-20: session 1 (continued): M3-03
- Delmar asked to keep going through every milestone that needs no human action without
  waiting for confirmation (the CLAUDE.md safety rules still apply).
- Catalogue v11 `items.camera` (EXIF/QuickTime make and model) and a decisions API
  (`set_decision`, `decision`). File-name dates: macOS screenshots (12-hour too), Telegram,
  Android, bare dates; Hypothesis round trips.
- `analyse.date_audit` flags missing, placeholder, future, upload-as-taken, conflicting
  (> 1 day) and out-of-sequence dates, and batches them by problem, camera, folder and prefix
  with per-item suggestions and a batch offset (`date-batch` groups).
- `analyse.date_fixes` applies one answer per batch (accept, keep, date, year → 1 July noon
  + `date-approx`, exact shift): `fix-date` / `plan-timewarp` actions store a `user` date.
  `planned_write` gives the importer ingest's plan with that date on top.
- Real sample (/tmp only): 1 batch of 3 (DESIGN, "Date audit").
- Tests: 888, 100%.
- Handoff: M3-06 must show date batches (reasons, current dates, suggestions, offset) and
  store the answer with `set_decision("group", id, "date", {...})`; M4-01 writes Google
  working copies from `planned_write` and runs timewarp for iCloud items with a `user` date.

### 2026-10-20: session 1 (continued): M3-02
- Ingest and index now embed every photo with open_clip (ViT-B-32, LAION-2B, MPS) and read
  faces with Apple Vision (capture quality) plus Core Image (eyes closed), from the same
  decode. Catalogue v10: `items.embedding` (float16) and `items.faces` (JSON, NULL = not
  measured). torch comes from the CPU wheel index on Linux.
- `analyse.best_shot`: groups within a 2-minute window by embedding similarity, ranks on
  sharpness, exposure, faces, eyes open and Apple's aesthetic score (weights in
  `[best_shot.weights]`, only scores every shot has), keeps the top two, proposes the rest
  for "To Delete" (`best-shot` groups). Without Vision, faces are left out.
- Tests never download the model: `ClipEmbedder` runs with random weights, grouping tests use
  `testing.embedder.ThumbnailEmbedder`, and an autouse fixture gives the CLI that stand-in.
  Vision is tested on the osxphotos test library.
- Real sample (/tmp only): ingest 4 min 43 s with CLIP and Vision. 216 groups, 351 shots
  proposed at the 0.94 default (661 at 0.90). See DESIGN for the measurements.
- Tests: 796, 100%.
- Handoff: the review gallery (M3-06) should show each best-shot group ranked, with the
  score parts from the group details, and let Delmar confirm or swap the kept two.

### 2026-10-20: session 1 (continued): M3-01
- `photomigrate.quality` measures photos (sharpness, exposure, contrast, size) and videos
  (brightness, motion) at ingest and index: one decode per photo for hashes and metrics, one
  ffmpeg pass per video for signature and metrics. Catalogue v9: `items.quality` and
  `quality_flags`.
- `analyse.quality` flags items with scores from 0 to 1; thresholds come from the new
  `--config` TOML file (`photomigrate.example.toml`).
- Runner fix: an action undone by a later run is applied again (a setting changed, then
  changed back, used to leave the middle setting in place).
- Real sample: 3,308 items measured. 29 photos flagged `blurry` (0.9%), nothing else. Dedupe
  results are unchanged by the single decode.
- Tests: 720, 100%.

### 2026-10-20: session 1 (continued): dedupe precision from a pixel check
- Delmar asked for a real image comparison (and not to trust filenames). Every proposed pair
  on the sample was decoded and compared by pixels.
  - The first rules gave 17 false positives out of 287.
  - The fixes: exact EXIF second; star groups around the keeper; a stricter 256-bit threshold
    when a time is missing.
  - Now 276 of 276 are confirmed the same image. Recall: 2 borderline possible misses out of
    2,258 similar ungrouped pairs.
- Tests: 665, 100%.

### 2026-10-20: session 1 (continued): M2-04 and a real-sample analysis
- `analyse.edited` (`edit-pair` groups, `--edited-policy`); catalogue v8 (`groups` rebuilt;
  migrations run with foreign keys off, then verified).
- Real sample (/tmp only):
  - ingest of the whole album took 80 s with all the hashing;
  - 1,562 edit pairs, no orphans;
  - 256 photo duplicate groups, mostly `IMG_1234.JPG` + `IMG_1234_2.jpg` re-uploads, which are
    true duplicates;
  - no video duplicates.
- The review gallery (M3-06) must show edit pairs side by side, with a bulk policy switch
  (commented on CLA-88).
- Tests: 662, 100%.

### 2026-10-20: session 1 (continued): M2-03
- Video signatures (`hashing.video_signature`, 8 sampled frames) and bitrate at ingest and index
  (catalogue v7). `analyse.dedupe_video` plus `analyse.grouping` (shared by both dedupe stages,
  which scope their groups by key prefix). `photomigrate analyse` runs both.
- Tests: 649, 100%.

### 2026-10-20: session 1 (continued): M2-02
- `analyse.dedupe_photo` plus `photomigrate analyse`; a 256-bit pHash (`phash_fine`, catalogue v6);
  Google photos are now hashed at ingest; `synthetic_image(jitter=…)` gives "a moment later" shots.
- The thresholds came from measurements (see DESIGN). The same-moment tolerance is 1 s, because
  copies keep identical EXIF times.
- Tests: 619, 100%.

### 2026-10-20: session 1 (continued): M2-01
- `index.icloud` plus `photomigrate index`; `hashing` (SHA-256, pHash/dHash); `safety.ReadOnlyGuard`
  and `announce_write`; catalogue v5 (scores, timezone).
- Finding: osxphotos' `mode=ro` SQLite reads still bump the library's `-shm` mtime. That
  led to the shadow-library design, so the real library is now untouched, mtimes included.
- Tests: 569, 100%. The macOS tests use osxphotos' test libraries (fetched into tmp,
  about 200 MB, pinned to v0.77.2).

### 2026-10-20: session 1 (continued): M1-06
- Live Photo pairing (`ingest.livephotos`, the `live-photos` stage run by `ingest`).
  Catalogue v4 adds `content_id`, the groups API and `add_flags`. `dates.moment_gap` and
  `same_moment` are shared with the date policy.
- Tests: 508, 100%. The pairing property test passed at 5,000 examples.
- Deferred to M2-02: marking a Google pair as a duplicate when iCloud already has that Live Photo.

### 2026-10-20: session 1 (continued): real-sample checks and M1-05
- Real-sample smoke test (approved by Delmar). It ran on this work Mac from `data-in/` into
  /tmp only; the zips were unchanged.
  - Matching: 3,307 of 3,308 matched. The one leftover pair is genuine: a video without a
    sidecar, and a sidecar whose video isn't in the export.
  - Apply took 21 s. There are 21 date conflicts and 171 sidecar-date fills, and no EXIF offsets
    at all (posted on CLA-85).
  - With M1-05: 1 shared album with 3,308 memberships and 16 distinct `People/` keywords.
- The Air is set up (`~/Code/photo-library-migration`, exiftool 13.55, ffmpeg 9, uv) and all
  tests pass there.
- M1-05: catalogue v3 adds albums, members, item_copies, keywords and google_url. Ingest
  resolves album copies into one item, and adds memberships, favourites, captions and People
  keywords.
- Tests: 472, 100%.

### 2026-10-20: session 1 (continued): M1-04 and a new plan
- Facts that changed the plan:
  - The Google export is **~600 GB** (12 × 50 GB parts), not 30–40 GB.
  - The run host is Delmar's home **MacBook Air** (`ssh openclaw`; macOS 27, 16 GB, about
    40 GB free internally) with an external APFS drive.
  - EC2/S3 was dropped: the only AWS profile is an Amazon Isengard account, and personal photos
    don't belong there.
  - Delmar keeps the existing export.
- What changed:
  - Ingest reads straight from the zips (`ingest.takeout`, `names`, `sidecars`, `exif`,
    `policy`, `stage`). The CLI has `ingest` plus the global `--catalogue` / `--plan-dir`.
    Catalogue v2 adds `metadata_plan`.
  - The safety guard forbids `data-in/` and judges relative names by the cwd only.
  - The Timeline export (`data-in/Timeline.json`, iOS format, 2025–2026 only) is surveyed;
    see the comment on CLA-87.
- Tests added / coverage: 448 tests, 100%. The Hypothesis property tests also passed at
  5,000 examples (`HYPOTHESIS_PROFILE=stress`).
- Decisions made:
  - Working copies are written at import (M4-01), not at ingest. Catalogue paths are relative
    (`<part>::<member>`).
  - A sidecar date goes into the home time zone with an `offset-assumed` flag.
    GPS-based time zones are a possible later improvement.
  - Altitude is written only together with latitude and longitude.
- Handoff notes for next session:
  - M1-05 adds album memberships, favourites, captions and people keywords; the people names
    are already in each ingest action's `sidecar_data`.
  - The real-sample smoke test is a dry run of `photomigrate ingest data-in` with
    `--catalogue` and `--plan-dir` under /tmp. Ask Delmar before running it.

### 2026-10-20: session 1 (continued): M1-03
- Issues worked: CLA-75 [M1-03].
- What changed: `photomigrate.catalogue` (schema v1, migrations, idempotent item and
  date-candidate upserts, nested transactions, in-memory snapshots) and `photomigrate.runner`
  (Action/Plan/Stage, dry-run vs apply, resume, plan JSON and rich tables). The guard now
  decodes percent-encoded sqlite `file:` URIs.
- Tests added / coverage: 262 tests, 100%.
- Decisions made:
  - Dry runs never open the catalogue for writing. They plan on an in-memory copy.
  - Re-running a finished stage writes nothing at all, not even a `stage_runs` row.
  - Group, decision and membership APIs are schema-only until the issues that use them
    (M1-05, M2, M3) add methods with their own tests.
- Handoff notes for next session:
  - M1-04 wires the CLI: global `--catalogue` / `--plan-dir` options and `ingest --apply`,
    using `run_stage` + `render_result`. Defaults are `runner.DEFAULT_CATALOGUE` and
    `DEFAULT_PLAN_DIR`.
  - Stage `apply` must be idempotent on disk: write only when the content differs.

### 2026-10-20: session 1 (continued): M1-02
- Issues worked: CLA-74 [M1-02].
- What changed:
  - `photomigrate.testing.media`: seeded synthetic images (JPEG, HEIC, PNG with EXIF date,
    offset, camera and GPS), animated GIFs, and tiny bit-exact H.264 MP4/MOV via ffmpeg.
  - `photomigrate.testing.takeout`: the naming rules, sidecar and album JSON, a
    `TakeoutBuilder` with a ground-truth `TakeoutManifest`, deterministic multi-part zips, and
    `standard_takeout()` with every quirk. Session fixture: `standard_takeout_zips`.
  - tests/fixtures/README.md (quirk catalogue). CI linux now installs ffmpeg and exiftool.
- Tests added / coverage: 219 tests in total, 100%.
- Decisions made:
  - The generator lives in `src/photomigrate/testing/`, not `tests/fixtures/` as in the
    playbook layout, so mypy --strict and the coverage gate apply to it.
  - Pillow and pillow-heif are runtime dependencies (the tech stack needs them later anyway).
  - Name truncation is a documented model (docs/TAKEOUT_FORMAT.md, "Modelled rules"); the
    real sample has no long names to confirm it.
- Handoff notes for next session:
  - Ingest tests should assert against `TakeoutManifest` (`metadata_sidecar()`, `parts`,
    relations), not re-derive the naming rules.
  - Calling `edited()` on a 47-character name makes the same name as the original, which
    raises "already in this Takeout". Real behaviour is unknown, so it is not modelled.

### 2026-10-20: session 1
- Issues worked: CLA-73 [M1-01].
- What changed:
  - Public repo `delmarolivier/photo-library-migration`; squash-merge only, branches deleted
    on merge, `main` protected (required checks, no direct pushes, admins included).
  - uv project (Python 3.12, src layout), Typer CLI with `--version` and stub stages
    (ingest, index, analyse, review, import, report).
  - CLAUDE.md, .claude/settings.json (verbatim from the playbook), .gitignore for media, data,
    Takeout, catalogue DBs and secrets.
  - pytest (branch coverage, 90% gate, `macos` marker, xdist, hypothesis profiles), ruff,
    mypy --strict, pre-commit.
  - `photomigrate.safety`: audit-hook media guard + pytest plugin, `announce_path`, and the
    AST no-delete check (`photomigrate-no-delete-check`).
  - GitHub Actions: `linux`, `no-delete-check`, `macos` (PRs only), combined `coverage` gate.
- Tests added / coverage: 113 tests, 100%.
- Decisions made:
  - Repo is **public**, not private as the playbook says (Delmar, 2026-10-20).
  - macOS CI job runs **on PRs only** (Delmar, 2026-10-20). The combined coverage gate needs
    it, so it is PR-only too; pushes to `main` run `linux` and `no-delete-check`.
  - Commits use the GitHub noreply email (repo-local git config), so no work email appears in
    public history.
  - The guard is an audit hook rather than a monkeypatch, so it also catches subprocesses
    (exiftool, ffmpeg) and sqlite3 (osxphotos). See docs/DESIGN.md.
  - Google-made creations go to the clutter review queue; Takeout people names become
    `People/<name>` keywords (Delmar, 2026-10-20; added to CLAUDE.md product decisions).
- Blockers / questions: see "Open questions" above.
- Handoff notes for next session:
  - Fixture media must be generated under `tmp_path`, or committed under `tests/fixtures/`
    (an allowed root). Media extensions are gitignored, so committed samples need a
    `.gitignore` negation for `tests/fixtures/`.
  - Any exiftool wrapper using stay-open mode must call `announce_path()` per file.
  - `data-in/` holds real Takeout samples (Delmar, 2026-10-20). Agreed use: a structural
    reference for M1-02 (see docs/TAKEOUT_FORMAT.md), then an opt-in, local-only, dry-run
    ingest smoke test after M1-04. It never runs in CI, and Delmar confirms before its first run.
    The survey scripts live outside the repo (/tmp/photomigrate-survey) and print shapes only.
