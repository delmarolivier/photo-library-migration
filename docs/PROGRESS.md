# Progress log

## Current state
- Milestone: 3. Quality and review
- Last completed issue: [M3-02] CLA-84 (best-shot picker).
- Next issue: [M3-03] CLA-85 (date audit).
- Coverage: 100% line + branch (gate 90%)
- Open questions for Delmar:
  - Best shot: `min_similarity` defaults to 0.94, measured on the sample (DESIGN, "Best
    shot"). Please judge the groups in the review gallery (M3-06); lower groups more.
  - The external APFS drive (2 TB or more) for the MacBook Air: still to be attached. Then
    download the 12 Takeout parts onto it before the Google links expire.
  - Before the first real `index` (CLAUDE.md rule 10):
    - move the Photos library onto the external drive and turn on "Download Originals";
    - give `sshd-keygen-wrapper` Full Disk Access (System Settings > Privacy & Security);
    - quit Photos while indexing;
    - give the go-ahead.

## Session log
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
